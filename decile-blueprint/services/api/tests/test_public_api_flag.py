"""Prompt 20's third acceptance criterion, and it needs no database.

    "The public API flag defaults OFF in every environment config in the repo."

So this scans the repository rather than trusting one default: the Pydantic setting, `.env.example`,
`.env` if a developer has one, the compose files, the CI workflows, the Playwright config and the
Makefile. Anything that mentions `BASKFY_PUBLIC_API_ENABLED` must set it to a false value.

It also asserts the *second* lock — that `create_app` does not mount the router when the
data-redistribution review is outstanding, whatever the flag says. `docs/DECISIONS.md` §20.1.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Final

import pytest
from fastapi.routing import APIRoute

from baskfy_api.app import create_app
from baskfy_api.routers import public
from baskfy_api.settings import Settings
from baskfy_core.public_api import DATA_REDISTRIBUTION_REVIEW, PUBLIC_API_PREFIX

REPO_ROOT: Final = Path(__file__).resolve().parents[3]

#: The exact variable. Matching a substring would also hit `NEXT_PUBLIC_API_URL`, which is the web
#: app's pointer at `/api/v1` and has nothing to do with the public tier.
FLAG: Final = "BASKFY_PUBLIC_API_ENABLED"

#: Anything a config file might spell "off" as.
FALSE_VALUES: Final = frozenset({"false", "0", "no", "off", '"false"', "'false'"})

#: Where an environment variable could be set. Deliberately wider than the files that currently
#: exist, so a new one is scanned the day it appears.
CONFIG_GLOBS: Final = (
    ".env",
    ".env.*",
    "Makefile",
    "infra/**/*.yml",
    "infra/**/*.yaml",
    ".github/workflows/*.yml",
    "apps/web/*.ts",
    "apps/web/**/*.env*",
    "docker-compose*.yml",
)

EXCLUDED_PARTS: Final = frozenset({"node_modules", ".venv", ".git", "__pycache__"})


def config_files() -> list[Path]:
    found: set[Path] = set()
    for pattern in CONFIG_GLOBS:
        for path in REPO_ROOT.glob(pattern):
            if path.is_file() and EXCLUDED_PARTS.isdisjoint(path.parts):
                found.add(path)
    return sorted(found)


def test_there_are_config_files_to_scan() -> None:
    """A scan over nothing would pass silently and prove nothing."""
    assert len(config_files()) > 3


def test_the_flag_defaults_off_in_the_settings_model() -> None:
    assert Settings.model_fields["public_api_enabled"].default is False
    assert Settings(environment="local").public_api_enabled is False


def test_the_flag_is_off_wherever_a_config_file_mentions_it() -> None:
    """Prompt 20's third acceptance criterion, over the whole repository."""
    pattern = re.compile(rf"{FLAG}\s*[:=]\s*(\S+)")
    offenders: list[str] = []
    for path in config_files():
        text = path.read_text(encoding="utf-8", errors="ignore")
        for number, line in enumerate(text.splitlines(), start=1):
            stripped = line.strip()
            if stripped.startswith("#") or FLAG not in stripped:
                continue
            match = pattern.search(stripped)
            if match is None:
                # A mention with no assignment — a comment inside a YAML block, say.
                continue
            value = match.group(1).strip().rstrip(",").lower()
            if value not in FALSE_VALUES:
                offenders.append(f"{path.relative_to(REPO_ROOT)}:{number}: {stripped}")
    assert offenders == [], f"{FLAG} is not off in: {offenders}"


def test_the_example_environment_names_the_flag_and_says_why(tmp_path: Path) -> None:
    """`.env.example` is where a new deployment starts. It must carry the flag *and* the reason."""
    del tmp_path
    text = (REPO_ROOT / ".env.example").read_text(encoding="utf-8")
    assert f"{FLAG}=false" in text
    assert "data-redistribution" in text


class TestTheRouterIsNotMounted:
    """The second lock. A flag turned on is not enough, and that is the point."""

    def test_is_enabled_is_false_with_the_flag_on_while_the_review_is_outstanding(self) -> None:
        assert DATA_REDISTRIBUTION_REVIEW.signed_off is False
        settings = Settings(environment="local", public_api_enabled=True)
        assert public.is_enabled(settings) is False

    @pytest.mark.parametrize("flag", [False, True])
    def test_no_public_route_exists_on_a_built_app(self, flag: bool) -> None:
        app = create_app(
            Settings(
                environment="local",
                jwt_secret="",
                rate_limit_enabled=False,
                public_api_enabled=flag,
            )
        )
        paths = [route.path for route in app.routes if isinstance(route, APIRoute)]
        assert [path for path in paths if path.startswith(PUBLIC_API_PREFIX)] == []

    def test_the_public_prefix_is_absent_from_the_openapi_document(self) -> None:
        """No route, no schema, and therefore nothing in the generated TypeScript client."""
        app = create_app(Settings(environment="local", jwt_secret="", rate_limit_enabled=False))
        document = app.openapi()
        paths = document["paths"]
        assert isinstance(paths, dict)
        assert [path for path in paths if str(path).startswith(PUBLIC_API_PREFIX)] == []
