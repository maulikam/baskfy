"""`providers doctor` (Prompt 2 deliverable 6, acceptance criterion 4).

    "`providers doctor` runs without credentials and reports each provider as unavailable rather
     than crashing."

That is the property under test throughout: an operator runs this command precisely when
something is misconfigured, so a stack trace is the one output it must never produce.
"""

from __future__ import annotations

import json

import pytest
from stubs import StubProvider

from baskfy_providers.cli import EXIT_OK, EXIT_UNAVAILABLE, main, run_doctor
from baskfy_providers.composite import CompositeProvider
from baskfy_providers.ports import BARS_CAPABILITIES, REFERENCE_CAPABILITIES, Capability
from baskfy_providers.settings import ProviderSettings


class TestWithoutCredentials:
    """Acceptance criterion 4, stated directly."""

    def test_it_exits_zero_and_reports_rather_than_crashing(
        self, settings: ProviderSettings
    ) -> None:
        code, output = run_doctor(settings)
        assert code == EXIT_OK
        assert "kite" in output

    def test_kite_is_reported_unavailable_with_the_reason(self, settings: ProviderSettings) -> None:
        _, output = run_doctor(settings)
        assert "[DOWN] kite" in output
        assert "BASKFY_KITE_API_KEY is not set" in output

    def test_it_prints_what_each_provider_can_currently_serve(
        self, settings: ProviderSettings
    ) -> None:
        """Prompt 2 §6: "prints what each provider can currently serve"."""
        _, output = run_doctor(settings)
        assert "serves now" in output
        assert "can serve" in output

    def test_capability_coverage_is_summarised(self, settings: ProviderSettings) -> None:
        _, output = run_doctor(settings)
        for capability in Capability:
            assert capability.value in output

    def test_json_output_is_machine_readable(self, settings: ProviderSettings) -> None:
        code, output = run_doctor(settings, as_json=True)
        assert code == EXIT_OK
        payload = json.loads(output)
        assert {p["name"] for p in payload["providers"]} >= {"kite", "nse"}
        assert payload["coverage"]["daily_bars"] is not None

    def test_require_all_exits_non_zero_when_something_is_down(
        self, settings: ProviderSettings
    ) -> None:
        """The CI/deploy-check mode: reporting is the default, failing is opt-in."""
        code, _ = run_doctor(settings, require_all=True)
        assert code == EXIT_UNAVAILABLE

    def test_an_adapter_whose_health_check_raises_is_reported_not_crashed(self) -> None:
        """A third-party adapter may throw while reporting its own health. Contain it."""
        stack = CompositeProvider(
            [
                StubProvider(
                    "broken", BARS_CAPABILITIES, check_raises=RuntimeError("driver exploded")
                )
            ]
        )
        code, output = run_doctor(stack=stack)
        assert code == EXIT_OK
        assert "[DOWN] broken" in output
        assert "RuntimeError" in output

    def test_a_stack_that_cannot_even_be_built_is_reported_not_raised(
        self, monkeypatch: pytest.MonkeyPatch, settings: ProviderSettings
    ) -> None:
        """The command must survive its own construction failing — that is exactly when an
        operator is running it."""

        def explode(_settings: ProviderSettings | None = None) -> CompositeProvider:
            raise RuntimeError("configuration is broken")

        monkeypatch.setattr("baskfy_providers.cli.build_provider_stack", explode)
        code, output = run_doctor(settings)
        assert code == EXIT_UNAVAILABLE
        assert "configuration is broken" in output

    def test_a_build_failure_is_still_machine_readable(
        self, monkeypatch: pytest.MonkeyPatch, settings: ProviderSettings
    ) -> None:
        def explode(_settings: ProviderSettings | None = None) -> CompositeProvider:
            raise RuntimeError("configuration is broken")

        monkeypatch.setattr("baskfy_providers.cli.build_provider_stack", explode)
        _, output = run_doctor(settings, as_json=True)
        assert "configuration is broken" in json.loads(output)["error"]


class TestWithProvidersUp:
    def test_all_available_exits_zero_under_require_all(self) -> None:
        stack = CompositeProvider(
            [
                StubProvider("kite", BARS_CAPABILITIES),
                StubProvider("nse", REFERENCE_CAPABILITIES),
            ]
        )
        code, output = run_doctor(require_all=True, stack=stack)
        assert code == EXIT_OK
        assert "All providers available." in output

    def test_a_down_provider_is_named_in_the_summary(self) -> None:
        stack = CompositeProvider(
            [
                StubProvider("kite", BARS_CAPABILITIES, available=False),
                StubProvider("nse", REFERENCE_CAPABILITIES),
            ]
        )
        _, output = run_doctor(stack=stack)
        assert "1 provider(s) unavailable: kite" in output

    def test_an_uncovered_capability_is_flagged(self) -> None:
        stack = CompositeProvider([StubProvider("kite", BARS_CAPABILITIES)])
        _, output = run_doctor(stack=stack)
        assert "UNAVAILABLE" in output


class TestArgumentParsing:
    def test_the_doctor_subcommand_runs(self, capsys: pytest.CaptureFixture[str]) -> None:
        assert main(["doctor"]) in (EXIT_OK, EXIT_UNAVAILABLE)
        assert "Decile providers" in capsys.readouterr().out

    def test_json_flag_is_accepted(self, capsys: pytest.CaptureFixture[str]) -> None:
        main(["doctor", "--json"])
        json.loads(capsys.readouterr().out)

    def test_an_unknown_subcommand_is_rejected(self) -> None:
        with pytest.raises(SystemExit):
            main(["diagnose"])

    def test_no_subcommand_is_rejected(self) -> None:
        with pytest.raises(SystemExit):
            main([])
