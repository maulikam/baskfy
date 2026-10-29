//! Python's `csv.reader(io.StringIO(text))` with the default `excel` dialect — CPython 3.12's
//! `_csv.c` state machine, fed the way the reader is fed: `StringIO` yields lines split after each
//! `\n` (no newline translation), and each line ends with the reader's end-of-line signal.
//!
//! What this reproduces and the importers depend on: a blank line is the row `[]`; `\r\n` ends a
//! record; quotes start a quoted field only at a field's start, `""` inside one is a quote, and a
//! character after a closing quote joins the field (`strict=False`); a newline inside quotes is
//! part of the field (so one row can span lines); a quoted field still open at the end of the
//! data is saved. The two `csv.Error`s the importers can meet are [`CsvError`] — a bare `\r`
//! inside a line, and a field over 131,072 characters — which no importer catches (a 500).

/// `csv.Error`, with Python's message.
#[derive(Debug, Clone, PartialEq, Eq)]
pub(crate) struct CsvError(pub(crate) String);

/// `csv.field_size_limit()` default.
const FIELD_LIMIT: usize = 131_072;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum State {
    StartRecord,
    StartField,
    InField,
    InQuotedField,
    QuoteInQuotedField,
    EatCrnl,
}

/// One input character, or the end-of-line signal the reader sends after each line.
#[derive(Clone, Copy)]
enum Input {
    Char(char),
    Eol,
}

struct Parser {
    state: State,
    field: String,
    field_len: usize,
    fields: Vec<String>,
}

impl Parser {
    fn save(&mut self) {
        self.fields.push(std::mem::take(&mut self.field));
        self.field_len = 0;
    }

    fn add(&mut self, c: char) -> Result<(), CsvError> {
        if self.field_len >= FIELD_LIMIT {
            return Err(CsvError(format!(
                "field larger than field limit ({FIELD_LIMIT})"
            )));
        }
        self.field.push(c);
        self.field_len += 1;
        Ok(())
    }

    fn process(&mut self, input: Input) -> Result<(), CsvError> {
        let newline = matches!(input, Input::Char('\n' | '\r'));
        let eol = matches!(input, Input::Eol);
        match self.state {
            State::StartRecord | State::StartField => {
                if self.state == State::StartRecord {
                    if eol {
                        return Ok(());
                    }
                    if newline {
                        self.state = State::EatCrnl;
                        return Ok(());
                    }
                    self.state = State::StartField;
                }
                match input {
                    Input::Eol => {
                        self.save();
                        self.state = State::StartRecord;
                    }
                    Input::Char('\n' | '\r') => {
                        self.save();
                        self.state = State::EatCrnl;
                    }
                    Input::Char('"') => self.state = State::InQuotedField,
                    Input::Char(',') => self.save(),
                    Input::Char(c) => {
                        self.add(c)?;
                        self.state = State::InField;
                    }
                }
            }
            State::InField => match input {
                Input::Eol => {
                    self.save();
                    self.state = State::StartRecord;
                }
                Input::Char('\n' | '\r') => {
                    self.save();
                    self.state = State::EatCrnl;
                }
                Input::Char(',') => {
                    self.save();
                    self.state = State::StartField;
                }
                Input::Char(c) => self.add(c)?,
            },
            State::InQuotedField => match input {
                Input::Eol => {}
                Input::Char('"') => self.state = State::QuoteInQuotedField,
                Input::Char(c) => self.add(c)?,
            },
            State::QuoteInQuotedField => match input {
                Input::Char('"') => {
                    self.add('"')?;
                    self.state = State::InQuotedField;
                }
                Input::Char(',') => {
                    self.save();
                    self.state = State::StartField;
                }
                Input::Eol => {
                    self.save();
                    self.state = State::StartRecord;
                }
                Input::Char('\n' | '\r') => {
                    self.save();
                    self.state = State::EatCrnl;
                }
                Input::Char(c) => {
                    self.add(c)?;
                    self.state = State::InField;
                }
            },
            State::EatCrnl => match input {
                Input::Char('\n' | '\r') => {}
                Input::Eol => self.state = State::StartRecord,
                Input::Char(_) => {
                    return Err(CsvError(
                        "new-line character seen in unquoted field - do you need to open the \
                         file with newline=''?"
                            .to_owned(),
                    ));
                }
            },
        }
        Ok(())
    }
}

/// `csv.reader(io.StringIO(text))`: rows on demand, as Python's lazy iteration raises.
pub(crate) struct Reader<'a> {
    lines: std::str::SplitInclusive<'a, char>,
    parser: Parser,
    done: bool,
}

impl<'a> Reader<'a> {
    /// A reader over `text`.
    pub(crate) fn new(text: &'a str) -> Self {
        Self {
            lines: text.split_inclusive('\n'),
            parser: Parser {
                state: State::StartRecord,
                field: String::new(),
                field_len: 0,
                fields: Vec::new(),
            },
            done: false,
        }
    }

    /// `next(reader)`: `Ok(None)` at the end.
    ///
    /// # Errors
    /// The `csv.Error` CPython raises on this row.
    pub(crate) fn next_row(&mut self) -> Result<Option<Vec<String>>, CsvError> {
        if self.done {
            return Ok(None);
        }
        let parser = &mut self.parser;
        loop {
            let Some(line) = self.lines.next() else {
                self.done = true;
                if parser.field_len != 0 || parser.state == State::InQuotedField {
                    parser.save();
                    return Ok(Some(std::mem::take(&mut parser.fields)));
                }
                return Ok(None);
            };
            for c in line.chars() {
                parser.process(Input::Char(c))?;
            }
            parser.process(Input::Eol)?;
            if parser.state == State::StartRecord {
                return Ok(Some(std::mem::take(&mut parser.fields)));
            }
        }
    }
}

/// `list(csv.reader(io.StringIO(text)))`.
///
/// # Errors
/// The `csv.Error` CPython raises, at the row it raises on.
pub(crate) fn read_all(text: &str) -> Result<Vec<Vec<String>>, CsvError> {
    let mut reader = Reader::new(text);
    let mut rows = Vec::new();
    while let Some(row) = reader.next_row()? {
        rows.push(row);
    }
    Ok(rows)
}

#[cfg(test)]
mod tests {
    #![allow(clippy::unwrap_used)]
    use super::*;

    fn rows(text: &str) -> Vec<Vec<&'static str>> {
        read_all(text)
            .unwrap()
            .into_iter()
            .map(|r| {
                r.into_iter()
                    .map(|s| &*Box::leak(s.into_boxed_str()))
                    .collect()
            })
            .collect()
    }

    #[test]
    fn rows_are_pythons() {
        // Each right-hand side is `list(csv.reader(io.StringIO(text)))` in CPython 3.12.
        assert_eq!(rows("a,b\n\nc\n"), vec![vec!["a", "b"], vec![], vec!["c"]]);
        assert_eq!(rows("a,b\r\nc,d\r\n"), vec![vec!["a", "b"], vec!["c", "d"]]);
        assert_eq!(rows("\"x,y\",z"), vec![vec!["x,y", "z"]]);
        assert_eq!(rows("\"a\"\"b\",c\n"), vec![vec!["a\"b", "c"]]);
        assert_eq!(rows("\"a\nb\",c\n"), vec![vec!["a\nb", "c"]]);
        assert_eq!(rows("\"ab\"cd,e\n"), vec![vec!["abcd", "e"]]);
        assert_eq!(rows("a\"b,c\n"), vec![vec!["a\"b", "c"]]);
        assert_eq!(rows("a,\n"), vec![vec!["a", ""]]);
        assert_eq!(rows("\"open"), vec![vec!["open"]]);
        assert_eq!(rows("  \n"), vec![vec!["  "]]);
        assert_eq!(rows(""), Vec::<Vec<&str>>::new());
        assert_eq!(rows("a\r"), vec![vec!["a"]]);
        assert!(read_all("a\rb\n").is_err());
    }
}
