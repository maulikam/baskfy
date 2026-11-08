//! Ports `baskfy_core.portfolio_graph` — nested portfolios: cycles, depth, assembly, and the
//! per-broker roll-up. Pure; the callers scope their rows to one user before calling.

use std::collections::{BTreeMap, HashMap, HashSet};

use baskfy_core::decimal::WideDecimal;

use super::num::zero;

/// `MAX_DEPTH`.
pub(crate) const MAX_DEPTH: usize = 6;
/// `ROOT_DEPTH`.
pub(crate) const ROOT_DEPTH: usize = 1;

/// `PortfolioGraphError` and its subclasses; `Display` is `str(exc)`.
#[derive(Debug, Clone, PartialEq, Eq)]
pub(crate) enum GraphError {
    /// `CycleError`.
    Cycle(Vec<i64>),
    /// `DepthExceededError`.
    DepthExceeded { portfolio_id: i64, depth: usize },
    /// `DuplicatePortfolioError`.
    Duplicate(i64),
    /// `UnknownPortfolioError`.
    Unknown(i64),
    /// `OrphanError`.
    Orphan {
        portfolio_id: i64,
        missing_parent_id: i64,
    },
    /// The base class's own "assembly lost portfolios".
    Lost { given: usize, out: usize },
}

impl std::fmt::Display for GraphError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            Self::Cycle(ids) => {
                let parts: Vec<String> = ids.iter().map(ToString::to_string).collect();
                write!(
                    f,
                    "portfolio parentage forms a cycle: {}",
                    parts.join(" -> ")
                )
            }
            Self::DepthExceeded {
                portfolio_id,
                depth,
            } => write!(
                f,
                "portfolio {portfolio_id} would sit at depth {depth}, deeper than the limit of \
                 {MAX_DEPTH}"
            ),
            Self::Duplicate(id) => write!(f, "portfolio {id} appears more than once"),
            Self::Unknown(id) => write!(f, "portfolio {id} is not in the given set"),
            Self::Orphan {
                portfolio_id,
                missing_parent_id,
            } => write!(
                f,
                "portfolio {portfolio_id} names parent {missing_parent_id}, which is not in the \
                 given set"
            ),
            Self::Lost { given, out } => {
                write!(f, "assembly lost portfolios: {given} in, {out} out")
            }
        }
    }
}

/// `PortfolioNode`.
#[derive(Debug, Clone, PartialEq, Eq)]
pub(crate) struct PortfolioNode {
    pub(crate) id: i64,
    pub(crate) name: String,
    pub(crate) parent_id: Option<i64>,
    pub(crate) broker_account_id: Option<i64>,
}

/// `Holding` (the graph's own: a row with a nullable broker).
#[derive(Debug, Clone)]
pub(crate) struct GraphHolding {
    pub(crate) portfolio_id: i64,
    pub(crate) broker_account_id: Option<i64>,
    pub(crate) quantity: WideDecimal,
    pub(crate) avg_price: WideDecimal,
}

/// `Totals`.
#[derive(Debug, Clone)]
pub(crate) struct Totals {
    pub(crate) quantity: WideDecimal,
    pub(crate) cost: WideDecimal,
    pub(crate) holdings: i64,
}

impl Default for Totals {
    fn default() -> Self {
        Self {
            quantity: zero(),
            cost: zero(),
            holdings: 0,
        }
    }
}

impl Totals {
    fn plus(&self, other: &Self) -> Self {
        Self {
            quantity: self.quantity.add(&other.quantity),
            cost: self.cost.add(&other.cost),
            holdings: self.holdings + other.holdings,
        }
    }
}

/// `BrokerLine`.
#[derive(Debug, Clone)]
pub(crate) struct BrokerLine {
    pub(crate) broker_account_id: i64,
    pub(crate) totals: Totals,
}

/// `BrokerRollup`.
#[derive(Debug, Clone)]
pub(crate) struct BrokerRollup {
    pub(crate) portfolio_id: i64,
    pub(crate) declared_broker_account_id: Option<i64>,
    pub(crate) by_broker: Vec<BrokerLine>,
    pub(crate) unattributed: Totals,
    pub(crate) total: Totals,
}

impl BrokerRollup {
    /// `spans_brokers`.
    pub(crate) fn spans_brokers(&self) -> bool {
        let places = self.by_broker.len() + usize::from(self.unattributed.holdings != 0);
        places > 1
    }

    /// `declaration_conflicts`.
    pub(crate) fn declaration_conflicts(&self) -> bool {
        let Some(declared) = self.declared_broker_account_id else {
            return false;
        };
        let ids: Vec<i64> = self.by_broker.iter().map(|l| l.broker_account_id).collect();
        ids != vec![declared] || self.unattributed.holdings > 0
    }
}

/// `TreeNode`.
#[derive(Debug, Clone)]
pub(crate) struct TreeNode {
    pub(crate) portfolio: PortfolioNode,
    pub(crate) children: Vec<TreeNode>,
    pub(crate) depth: usize,
}

/// `Orphan`.
#[derive(Debug, Clone)]
pub(crate) struct Orphan {
    pub(crate) subtree: TreeNode,
}

/// `Forest`.
#[derive(Debug, Clone)]
pub(crate) struct Forest {
    pub(crate) roots: Vec<TreeNode>,
    pub(crate) orphans: Vec<Orphan>,
}

fn count(node: &TreeNode) -> usize {
    1 + node.children.iter().map(count).sum::<usize>()
}

/// `_index`: input order kept, duplicates refused.
struct Index<'a> {
    order: Vec<i64>,
    by_id: HashMap<i64, &'a PortfolioNode>,
}

impl<'a> Index<'a> {
    fn new(rows: &'a [PortfolioNode]) -> Result<Self, GraphError> {
        let mut order = Vec::with_capacity(rows.len());
        let mut by_id = HashMap::with_capacity(rows.len());
        for row in rows {
            if by_id.insert(row.id, row).is_some() {
                return Err(GraphError::Duplicate(row.id));
            }
            order.push(row.id);
        }
        Ok(Self { order, by_id })
    }

    fn parent(&self, id: i64) -> Option<i64> {
        self.by_id.get(&id).and_then(|n| n.parent_id)
    }

    fn contains(&self, id: i64) -> bool {
        self.by_id.contains_key(&id)
    }
}

/// `find_cycle`.
fn find_cycle(index: &Index<'_>) -> Option<Vec<i64>> {
    let mut acyclic: HashSet<i64> = HashSet::new();
    for &start in &index.order {
        let mut seen: HashMap<i64, usize> = HashMap::new();
        let mut path: Vec<i64> = Vec::new();
        let mut current = Some(start);
        while let Some(c) = current {
            if !index.contains(c) || acyclic.contains(&c) {
                break;
            }
            if let Some(&at) = seen.get(&c) {
                let mut ring = path[at..].to_vec();
                ring.push(c);
                return Some(ring);
            }
            seen.insert(c, path.len());
            path.push(c);
            current = index.parent(c);
        }
        acyclic.extend(path);
    }
    None
}

fn check_no_cycles(index: &Index<'_>) -> Result<(), GraphError> {
    find_cycle(index).map_or(Ok(()), |c| Err(GraphError::Cycle(c)))
}

/// `_chain`: the ids walked upward, and the dangling parent if the chain does not end at a root.
fn chain(index: &Index<'_>, portfolio_id: i64) -> Result<(Vec<i64>, Option<i64>), GraphError> {
    if !index.contains(portfolio_id) {
        return Err(GraphError::Unknown(portfolio_id));
    }
    let mut walked = Vec::new();
    let mut current = portfolio_id;
    loop {
        walked.push(current);
        match index.parent(current) {
            None => return Ok((walked, None)),
            Some(p) if !index.contains(p) => return Ok((walked, Some(p))),
            Some(p) => current = p,
        }
    }
}

/// `depth_of`.
pub(crate) fn depth_of(rows: &[PortfolioNode], portfolio_id: i64) -> Result<usize, GraphError> {
    let index = Index::new(rows)?;
    check_no_cycles(&index)?;
    let (walked, dangling) = chain(&index, portfolio_id)?;
    if let Some(parent) = dangling {
        return Err(GraphError::Orphan {
            portfolio_id: walked.last().copied().unwrap_or(portfolio_id),
            missing_parent_id: parent,
        });
    }
    Ok(walked.len())
}

fn children_by_parent<'a>(index: &Index<'a>) -> HashMap<i64, Vec<&'a PortfolioNode>> {
    let mut grouped: HashMap<i64, Vec<&PortfolioNode>> = HashMap::new();
    for id in &index.order {
        if let Some(node) = index.by_id.get(id)
            && let Some(parent) = node.parent_id
            && index.contains(parent)
        {
            grouped.entry(parent).or_default().push(node);
        }
    }
    grouped
}

fn height(children: &HashMap<i64, Vec<&PortfolioNode>>, node_id: i64) -> usize {
    match children.get(&node_id) {
        None => 1,
        Some(below) => {
            1 + below
                .iter()
                .map(|c| height(children, c.id))
                .max()
                .unwrap_or(0)
        }
    }
}

/// `check_move(rows, portfolio_id, new_parent_id)`: refuse a ring or a breach of the depth cap.
pub(crate) fn check_move(
    rows: &[PortfolioNode],
    portfolio_id: i64,
    new_parent_id: Option<i64>,
) -> Result<(), GraphError> {
    let index = Index::new(rows)?;
    check_no_cycles(&index)?;
    if !index.contains(portfolio_id) {
        return Err(GraphError::Unknown(portfolio_id));
    }
    let tall = height(&children_by_parent(&index), portfolio_id);
    let Some(parent) = new_parent_id else {
        if tall > MAX_DEPTH {
            return Err(GraphError::DepthExceeded {
                portfolio_id,
                depth: tall,
            });
        }
        return Ok(());
    };
    if parent == portfolio_id {
        return Err(GraphError::Cycle(vec![portfolio_id, portfolio_id]));
    }
    if !index.contains(parent) {
        return Err(GraphError::Orphan {
            portfolio_id,
            missing_parent_id: parent,
        });
    }
    let inside = descendant_ids(rows, portfolio_id)?;
    if inside.contains(&parent) {
        let (upward, _) = chain(&index, parent)?;
        let cut = upward
            .iter()
            .position(|id| *id == portfolio_id)
            .map_or(upward.len(), |p| p + 1);
        let mut ring = vec![portfolio_id];
        ring.extend_from_slice(&upward[..cut]);
        return Err(GraphError::Cycle(ring));
    }
    let (walk, dangling) = chain(&index, parent)?;
    if let Some(missing) = dangling {
        return Err(GraphError::Orphan {
            portfolio_id: walk.last().copied().unwrap_or(parent),
            missing_parent_id: missing,
        });
    }
    let deepest = walk.len() + tall;
    if deepest > MAX_DEPTH {
        return Err(GraphError::DepthExceeded {
            portfolio_id,
            depth: deepest,
        });
    }
    Ok(())
}

/// `descendant_ids`: breadth-first, root first.
pub(crate) fn descendant_ids(rows: &[PortfolioNode], root_id: i64) -> Result<Vec<i64>, GraphError> {
    let index = Index::new(rows)?;
    check_no_cycles(&index)?;
    if !index.contains(root_id) {
        return Err(GraphError::Unknown(root_id));
    }
    let children = children_by_parent(&index);
    let mut found = vec![root_id];
    let mut cursor = 0;
    while cursor < found.len() {
        if let Some(kids) = children.get(&found[cursor]) {
            found.extend(kids.iter().map(|c| c.id));
        }
        cursor += 1;
    }
    Ok(found)
}

fn build(
    children: &HashMap<i64, Vec<&PortfolioNode>>,
    node: &PortfolioNode,
    depth: usize,
) -> Result<TreeNode, GraphError> {
    if depth > MAX_DEPTH {
        return Err(GraphError::DepthExceeded {
            portfolio_id: node.id,
            depth,
        });
    }
    let mut built = Vec::new();
    for child in children.get(&node.id).map(Vec::as_slice).unwrap_or(&[]) {
        built.push(build(children, child, depth + 1)?);
    }
    Ok(TreeNode {
        portfolio: node.clone(),
        children: built,
        depth,
    })
}

/// `assemble_tree(rows)` with the default depth cap.
pub(crate) fn assemble_tree(rows: &[PortfolioNode]) -> Result<Forest, GraphError> {
    let index = Index::new(rows)?;
    check_no_cycles(&index)?;
    let children = children_by_parent(&index);
    let mut roots = Vec::new();
    let mut orphans = Vec::new();
    for id in &index.order {
        let Some(node) = index.by_id.get(id) else {
            continue;
        };
        match node.parent_id {
            None => roots.push(build(&children, node, ROOT_DEPTH)?),
            Some(parent) if !index.contains(parent) => orphans.push(Orphan {
                subtree: build(&children, node, ROOT_DEPTH)?,
            }),
            Some(_) => {}
        }
    }
    let size = roots.iter().map(count).sum::<usize>()
        + orphans.iter().map(|o| count(&o.subtree)).sum::<usize>();
    if size != index.order.len() {
        return Err(GraphError::Lost {
            given: index.order.len(),
            out: size,
        });
    }
    Ok(Forest { roots, orphans })
}

/// `rollup_by_broker`.
pub(crate) fn rollup_by_broker(
    rows: &[PortfolioNode],
    holdings: &[GraphHolding],
    root_id: i64,
) -> Result<BrokerRollup, GraphError> {
    let inside: HashSet<i64> = descendant_ids(rows, root_id)?.into_iter().collect();
    let index = Index::new(rows)?;
    let mut per_broker: BTreeMap<i64, Totals> = BTreeMap::new();
    let mut unattributed = Totals::default();
    for holding in holdings {
        if !inside.contains(&holding.portfolio_id) {
            continue;
        }
        let line = Totals {
            quantity: holding.quantity.clone(),
            cost: holding.quantity.mul(&holding.avg_price),
            holdings: 1,
        };
        match holding.broker_account_id {
            None => unattributed = unattributed.plus(&line),
            Some(b) => {
                let next = per_broker.get(&b).cloned().unwrap_or_default().plus(&line);
                per_broker.insert(b, next);
            }
        }
    }
    let by_broker: Vec<BrokerLine> = per_broker
        .into_iter()
        .map(|(broker_account_id, totals)| BrokerLine {
            broker_account_id,
            totals,
        })
        .collect();
    let mut total = unattributed.clone();
    for entry in &by_broker {
        total = total.plus(&entry.totals);
    }
    Ok(BrokerRollup {
        portfolio_id: root_id,
        declared_broker_account_id: index.by_id.get(&root_id).and_then(|n| n.broker_account_id),
        by_broker,
        unattributed,
        total,
    })
}

#[cfg(test)]
mod tests {
    use baskfy_core::decimal::WideDecimal;

    fn lit(text: &str) -> WideDecimal {
        text.parse().unwrap_or_default()
    }

    fn text(value: &WideDecimal) -> String {
        value.to_sci_string()
    }
    use super::*;

    fn node(id: i64, parent: Option<i64>, broker: Option<i64>) -> PortfolioNode {
        PortfolioNode {
            id,
            name: format!("p{id}"),
            parent_id: parent,
            broker_account_id: broker,
        }
    }

    #[test]
    fn cycles_depth_and_orphans() {
        let ring = [node(7, Some(9), None), node(9, Some(7), None)];
        assert_eq!(
            assemble_tree(&ring).map(|_| ()).map_err(|e| e.to_string()),
            Err("portfolio parentage forms a cycle: 7 -> 9 -> 7".to_owned())
        );
        let selfish = [node(3, Some(3), None)];
        assert_eq!(
            depth_of(&selfish, 3).map_err(|e| e.to_string()),
            Err("portfolio parentage forms a cycle: 3 -> 3".to_owned())
        );
        let chain: Vec<PortfolioNode> = (1..=7)
            .map(|i| node(i, if i == 1 { None } else { Some(i - 1) }, None))
            .collect();
        assert_eq!(
            assemble_tree(&chain).map(|_| ()).map_err(|e| e.to_string()),
            Err("portfolio 7 would sit at depth 7, deeper than the limit of 6".to_owned())
        );
        assert_eq!(depth_of(&chain[..6], 6), Ok(6));
        let orphaned = [
            node(1, None, None),
            node(2, Some(99), None),
            node(3, Some(2), None),
        ];
        let forest = assemble_tree(&orphaned).unwrap_or(Forest {
            roots: vec![],
            orphans: vec![],
        });
        assert_eq!(forest.roots.len(), 1);
        assert_eq!(forest.orphans.len(), 1);
        assert_eq!(forest.orphans[0].subtree.children[0].depth, 2);
        assert_eq!(
            depth_of(&orphaned, 3).map_err(|e| e.to_string()),
            Err("portfolio 2 names parent 99, which is not in the given set".to_owned())
        );
        assert_eq!(descendant_ids(&orphaned, 2), Ok(vec![2, 3]));
    }

    #[test]
    fn rollup_keeps_unattributed_apart() {
        let rows = [
            node(1, None, Some(10)),
            node(2, Some(1), None),
            node(3, None, None),
        ];
        let h = |p, b, q: &str, a: &str| GraphHolding {
            portfolio_id: p,
            broker_account_id: b,
            quantity: lit(q),
            avg_price: lit(a),
        };
        let holdings = [
            h(1, Some(10), "10.0000", "100.5000"),
            h(2, Some(11), "5.0000", "20.0000"),
            h(2, None, "1.0000", "1.0000"),
            h(3, Some(10), "99.0000", "99.0000"),
        ];
        let Ok(rollup) = rollup_by_broker(&rows, &holdings, 1) else {
            unreachable!("a valid subtree")
        };
        assert_eq!(rollup.by_broker.len(), 2);
        assert_eq!(text(&rollup.by_broker[0].totals.cost), "1005.00000000");
        assert_eq!(text(&rollup.total.quantity), "16.0000");
        assert_eq!(rollup.unattributed.holdings, 1);
        assert!(rollup.spans_brokers());
        assert!(rollup.declaration_conflicts());
    }

    #[test]
    fn a_move_is_refused_for_a_ring_or_a_breach() {
        // Ports test_portfolio_graph.py's check_move assertions.
        let node = |id: i64, parent: Option<i64>| PortfolioNode {
            id,
            name: id.to_string(),
            parent_id: parent,
            broker_account_id: None,
        };
        let rows = vec![node(1, None), node(2, Some(1)), node(3, Some(2))];
        assert_eq!(
            check_move(&rows, 1, Some(3)).map_err(|e| e.to_string()),
            Err("portfolio parentage forms a cycle: 1 -> 3 -> 2 -> 1".to_owned())
        );
        assert!(matches!(
            check_move(&rows, 1, Some(1)),
            Err(GraphError::Cycle(_))
        ));
        assert_eq!(check_move(&rows, 3, None), Ok(()));
        let deep: Vec<PortfolioNode> = (1..=6)
            .map(|i| node(i, if i == 1 { None } else { Some(i - 1) }))
            .chain([node(7, None), node(8, Some(7))])
            .collect();
        assert!(matches!(
            check_move(&deep, 7, Some(5)),
            Err(GraphError::DepthExceeded { depth: 7, .. })
        ));
    }
}
