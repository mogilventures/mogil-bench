from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.core import TyperOption
from typer.main import get_command

from mogil_bench.cli import app
from mogil_bench.harbor_tasks import translate_harbor_task
from mogil_bench.models import create_attempt_identity
from mogil_bench.packs import load_pack
from mogil_bench.parity import (
    run_live_parity,
    validate_parity_output,
    validate_parity_pack,
    validate_secret_inventory,
)

ROOT = Path(__file__).parents[1]
PACK_PATH = ROOT / "packs/daytona-provider-parity-v1.yaml"
IMAGE = (
    "ghcr.io/mogilventures/mogil-bench-daytona-runtime@sha256:"
    "7728671c38220e066d23f63fd2544cc0722874ec40e1c86c883c8cc4d6c35dfe"
)
TASK_IDS = (
    "correct-fictional-calculator",
    "normalize-widget-slugs",
    "summarize-fictional-inventory",
)
CONFIG_IDS = ("anthropic-direct", "openrouter-routed")


def test_parity_pack_is_same_model_with_provider_specific_restricted_secrets(
    tmp_path: Path,
) -> None:
    pack = load_pack(PACK_PATH)
    validate_parity_pack(pack)

    assert [task.id for task in pack.tasks] == list(TASK_IDS)
    assert [(config.id, config.provider, config.model) for config in pack.configurations] == [
        ("anthropic-direct", "anthropic", "claude-sonnet-4-6"),
        ("openrouter-routed", "openrouter", "anthropic/claude-sonnet-4.6"),
    ]
    expected = {
        "anthropic-direct": ("api.anthropic.com", "ANTHROPIC_API_KEY", "mogil-anthropic-smoke"),
        "openrouter-routed": ("openrouter.ai", "OPENROUTER_API_KEY", "mogil-openrouter-parity"),
    }
    serialized = PACK_PATH.read_text(encoding="utf-8")
    assert "sk-ant-" not in serialized and "sk-or-" not in serialized

    for config in pack.configurations:
        host, env_name, secret_name = expected[config.id]
        policy = config.environment_policy
        assert policy is not None
        assert policy.image == IMAGE
        assert policy.allowed_hosts == [host]
        assert policy.secret_refs == {env_name: f"ref:{secret_name}"}
        fixture = (PACK_PATH.parent / str(pack.tasks[0].fixture)).resolve()
        hidden = tmp_path / f"{config.id}-verify.py"
        hidden.write_text((fixture / "verify.py").read_text(encoding="utf-8"), encoding="utf-8")
        translation = translate_harbor_task(
            PACK_PATH,
            pack.tasks[0],
            config,
            create_attempt_identity(
                "logical", attempt_id_factory=lambda config_id=config.id: config_id
            ),
            tmp_path / "translations",
            hidden_verifier=hidden,
        )
        assert translation.environment_config["kwargs"]["secrets"] == {env_name: secret_name}  # type: ignore[index]
        assert secret_name not in (translation.task_dir / "task.toml").read_text(encoding="utf-8")


def test_parity_pack_validation_rejects_policy_or_task_drift() -> None:
    pack = load_pack(PACK_PATH)
    routed = pack.configurations[1]
    assert routed.environment_policy is not None
    unsafe_policy = routed.environment_policy.model_copy(
        update={"allowed_hosts": ["openrouter.ai", "example.com"]}
    )
    with pytest.raises(ValueError, match="configuration contract"):
        validate_parity_pack(
            pack.model_copy(
                update={
                    "configurations": [
                        pack.configurations[0],
                        routed.model_copy(update={"environment_policy": unsafe_policy}),
                    ]
                }
            )
        )
    with pytest.raises(ValueError, match="task contract"):
        validate_parity_pack(
            pack.model_copy(
                update={
                    "tasks": [
                        pack.tasks[0].model_copy(update={"fixture": "fixtures/other"}),
                        *pack.tasks[1:],
                    ]
                }
            )
        )


def test_cli_exposes_bounded_attempt_count() -> None:
    run_command = get_command(app).commands["run"]
    option = next(parameter for parameter in run_command.params if parameter.name == "attempts")

    assert isinstance(option, TyperOption)
    assert option.opts == ["--attempts"]
    assert option.default == 1
    assert (option.type.min, option.type.max) == (1, 10)


def test_live_parity_is_manually_gated_before_network_or_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("MOGIL_RUN_DAYTONA_PARITY", raising=False)
    called = False

    def forbidden_inventory() -> dict[str, tuple[str, ...]]:
        nonlocal called
        called = True
        raise AssertionError("must not contact Daytona before authorization")

    with pytest.raises(PermissionError, match="authorize the paid 18-attempt"):
        run_live_parity(
            tmp_path / "run",
            runner=lambda *_args, **_kwargs: tmp_path / "run",
            inventory_loader=forbidden_inventory,
        )
    assert called is False
    assert not (tmp_path / "run").exists()


def test_live_parity_checks_inventory_and_fixed_runner_arguments(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MOGIL_RUN_DAYTONA_PARITY", "1")
    monkeypatch.setenv("DAYTONA_API_KEY", "manager-credential")
    calls: list[tuple[Path, Path, bool, int]] = []

    def runner(pack: Path, output: Path, *, allow_agents: bool, attempts: int) -> Path:
        calls.append((pack, output, allow_agents, attempts))
        return output

    with pytest.raises(ValueError, match="parity output is absent"):
        run_live_parity(
            tmp_path / "run",
            runner=runner,
            inventory_loader=lambda: {
                "mogil-anthropic-smoke": ("api.anthropic.com",),
                "mogil-openrouter-parity": ("openrouter.ai",),
            },
        )
    assert calls == [(PACK_PATH, tmp_path / "run", True, 3)]


def test_live_parity_secret_inventory_is_exact_and_never_contains_values() -> None:
    validate_secret_inventory(
        {
            "mogil-anthropic-smoke": ("api.anthropic.com",),
            "mogil-openrouter-parity": ("openrouter.ai",),
        }
    )
    with pytest.raises(ValueError, match="absent: mogil-openrouter-parity"):
        validate_secret_inventory({"mogil-anthropic-smoke": ("api.anthropic.com",)})
    with pytest.raises(ValueError, match="restricted exactly"):
        validate_secret_inventory(
            {
                "mogil-anthropic-smoke": ("api.anthropic.com",),
                "mogil-openrouter-parity": (),
            }
        )


def test_complete_parity_output_requires_18_blinded_quality_attempts(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    fixture = json.loads(
        (ROOT / "tests/fixtures/daytona-reviewer-contract.json").read_text(encoding="utf-8")
    )
    results: list[dict[str, object]] = []
    evidence: list[dict[str, object]] = []
    for config in CONFIG_IDS:
        for task in TASK_IDS:
            logical = f"logical-{config}-{task}"
            for number in range(1, 4):
                attempt = f"attempt-{config}-{task}-{number}"
                results.append(
                    {
                        "logical_run_id": logical,
                        "attempt_id": attempt,
                        "attempt_number": number,
                        "task_id": task,
                        "configuration_id": config,
                    }
                )
                artifact = json.loads(json.dumps(fixture))
                artifact["run"]["id"] = logical
                artifact["run"]["attempt"] = attempt
                artifact["reviewer"]["task"]["id"] = task
                evidence.append(artifact)
    (run_dir / "manifest.json").write_text(
        json.dumps({"result_count": 18, "results": results}), encoding="utf-8"
    )
    (run_dir / "mogil.harbor-evidence.json").write_text(
        json.dumps(evidence), encoding="utf-8"
    )
    (run_dir / "mogil.harbor-evidence.jsonl").write_text(
        "".join(json.dumps(item) + "\n" for item in evidence), encoding="utf-8"
    )

    validate_parity_output(run_dir, credential_values=("manager-credential",))

    evidence[0]["reviewer"]["task"]["prompt"] = "anthropic provenance"
    (run_dir / "mogil.harbor-evidence.json").write_text(
        json.dumps(evidence), encoding="utf-8"
    )
    with pytest.raises(ValueError, match="reviewer projection contains provenance"):
        validate_parity_output(run_dir)
