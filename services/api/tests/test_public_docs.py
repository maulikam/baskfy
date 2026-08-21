"""The interactive API reference — Prompt 20 deliverable 5. No database needed.

    "Docs: an interactive API reference (Scalar or Redoc) served from the OpenAPI spec, with
     copy-paste examples in curl, Python and TypeScript."

The examples live inside the OpenAPI document as ``x-codeSamples``, which Redoc renders natively.
That makes them assertable, which is what these tests do: **every** public operation carries all
three languages, each one names the ``X-API-Key`` header, and none of them contains a real secret.
"""

from __future__ import annotations

from collections.abc import Mapping

import api_helpers
import pytest

from decile_api import public_docs
from decile_api.app import create_app
from decile_api.settings import Settings
from decile_core.public_api import PUBLIC_API_PREFIX, PUBLIC_API_VERSION

BASE_URL = "https://api.decile.in"

#: The three Prompt 20 §5 names, spelled as Redoc's ``lang`` values.
EXPECTED_LANGS = ["curl", "Python", "JavaScript"]


def document_with_public_routes() -> Mapping[str, object]:
    """The service's OpenAPI document, taken from an app that mounted the public router.

    ``api_helpers.review_signed_off`` is the same trick ``test_public_api.py`` uses: the gate is
    shut in every committed build (`test_public_api_flag.py` asserts it), so a test of the public
    reference has to open it deliberately.
    """
    with api_helpers.review_signed_off():
        app = create_app(
            Settings(
                environment="local",
                jwt_secret="",
                rate_limit_enabled=False,
                public_api_enabled=True,
            )
        )
        return app.openapi()


def public_document() -> Mapping[str, object]:
    return public_docs.public_document(document_with_public_routes(), base_url=BASE_URL)


def operations() -> list[tuple[str, str, Mapping[str, object]]]:
    paths = public_document()["paths"]
    assert isinstance(paths, Mapping)
    found: list[tuple[str, str, Mapping[str, object]]] = []
    for path, item in paths.items():
        assert isinstance(item, Mapping)
        for method, operation in item.items():
            assert isinstance(operation, Mapping)
            found.append((str(path), str(method), operation))
    return found


class TestTheFilteredDocument:
    def test_it_carries_only_the_public_routes(self) -> None:
        paths = public_document()["paths"]
        assert isinstance(paths, Mapping)
        assert paths, "the public document should not be empty when the router is mounted"
        assert all(str(path).startswith(PUBLIC_API_PREFIX) for path in paths)

    def test_it_names_its_own_version(self) -> None:
        info = public_document()["info"]
        assert isinstance(info, Mapping)
        assert info["version"] == PUBLIC_API_VERSION

    def test_it_points_at_this_deployment(self) -> None:
        servers = public_document()["servers"]
        assert servers == [{"url": BASE_URL}]

    def test_the_description_says_what_is_withheld(self) -> None:
        """A reader must not have to fetch `/terms` to learn there are no prices in here."""
        info = public_document()["info"]
        assert isinstance(info, Mapping)
        description = str(info["description"])
        assert "withheld_fields" in description
        assert "not a SEBI-registered investment adviser" in description

    def test_the_terms_endpoint_is_in_it(self) -> None:
        paths = public_document()["paths"]
        assert isinstance(paths, Mapping)
        assert f"{PUBLIC_API_PREFIX}/terms" in paths

    def test_the_document_is_byte_stable(self) -> None:
        """Two renders on two machines must produce the same bytes — same rule as `make openapi`."""
        first = public_docs.render_document(public_document())
        second = public_docs.render_document(public_document())
        assert first == second


class TestCodeSamples:
    def test_there_are_operations_to_check(self) -> None:
        assert len(operations()) >= 4

    def test_every_operation_carries_all_three_languages(self) -> None:
        for path, method, operation in operations():
            samples = operation.get(public_docs.CODE_SAMPLE_KEY)
            assert isinstance(samples, list), f"{method} {path} has no code samples"
            assert [sample["lang"] for sample in samples] == EXPECTED_LANGS

    def test_every_sample_authenticates_with_the_header_docs_07_names(self) -> None:
        for path, method, operation in operations():
            samples = operation[public_docs.CODE_SAMPLE_KEY]
            assert isinstance(samples, list)
            for sample in samples:
                assert "X-API-Key" in sample["source"], f"{method} {path} / {sample['lang']}"

    def test_every_sample_targets_the_configured_base_url(self) -> None:
        for _, _, operation in operations():
            samples = operation[public_docs.CODE_SAMPLE_KEY]
            assert isinstance(samples, list)
            for sample in samples:
                assert BASE_URL in sample["source"]

    def test_path_parameters_are_filled_in_so_a_sample_is_runnable(self) -> None:
        """A sample containing a literal `{public_id}` is one nobody can paste and run."""
        for path, _, operation in operations():
            samples = operation[public_docs.CODE_SAMPLE_KEY]
            assert isinstance(samples, list)
            for sample in samples:
                assert "{public_id}" not in sample["source"], path
                assert "{symbol}" not in sample["source"], path

    def test_no_sample_carries_a_real_looking_secret(self) -> None:
        for _, _, operation in operations():
            samples = operation[public_docs.CODE_SAMPLE_KEY]
            assert isinstance(samples, list)
            for sample in samples:
                assert public_docs.PLACEHOLDER_KEY in sample["source"]
                assert "YOUR_SECRET_HERE" in sample["source"]


class TestTheReferencePage:
    def test_it_points_redoc_at_the_public_spec(self) -> None:
        page = public_docs.redoc_page(
            spec_url=f"{PUBLIC_API_PREFIX}/openapi.json",
            script_url="https://cdn.example/redoc.js",
        )
        assert f'spec-url="{PUBLIC_API_PREFIX}/openapi.json"' in page
        assert 'src="https://cdn.example/redoc.js"' in page

    def test_the_script_url_is_pinned_to_a_version(self) -> None:
        """An unpinned CDN URL is a third party choosing what executes on our documentation page."""
        url = Settings(environment="local").redoc_script_url
        assert "/v" in url, url

    def test_a_hostile_setting_cannot_break_out_of_the_attribute(self) -> None:
        page = public_docs.redoc_page(
            spec_url='"><script>alert(1)</script>', script_url="https://cdn.example/redoc.js"
        )
        assert "<script>alert(1)</script>" not in page

    @pytest.mark.parametrize("marker", ["<redoc", "noindex"])
    def test_the_page_is_a_reference_and_not_indexable(self, marker: str) -> None:
        page = public_docs.redoc_page(spec_url="/spec.json", script_url="https://cdn/redoc.js")
        assert marker in page
