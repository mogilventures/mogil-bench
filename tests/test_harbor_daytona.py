from __future__ import annotations

import asyncio
import json
import subprocess
import tomllib
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from harbor.environments.base import ExecResult
from harbor.models.task.config import (
    EnvironmentConfig as HarborTaskEnvironmentConfig,
)
from harbor.models.task.config import NetworkMode as HarborNetworkMode
from harbor.models.task.config import NetworkPolicy
from harbor.models.trial.paths import TrialPaths
from pydantic import ValidationError
from test_harbor_backend import RecordingJob
from test_harbor_translation import inputs

import mogil_bench.harbor_daytona as harbor_daytona
from mogil_bench.evidence import validate_evidence_artifact
from mogil_bench.harbor_backend import (
    _FIXTURE_PROFILE_TOKEN,
    HARBOR_VERSION,
    HarborBackend,
    PreflightError,
    PreflightProbes,
    preflight,
)
from mogil_bench.harbor_daytona import (
    ManagedSandbox,
    MogilDaytonaEnvironment,
    reap_expired,
)
from mogil_bench.harbor_tasks import translate_harbor_task
from mogil_bench.models import Configuration, EvidenceStatus, Harness, create_attempt_identity

ROOT = Path(__file__).parents[1]
IMAGE = "ghcr.io/mogilventures/bench-daytona@sha256:" + "a" * 64


def daytona_configuration(**policy_overrides: object) -> Configuration:
    policy: dict[str, object] = {
        "image": IMAGE,
        "cpus": 2,
        "memory_mb": 4096,
        "storage_mb": 8192,
        "network_mode": "allowlist",
        "allowed_hosts": ["api.anthropic.com"],
        "secret_refs": {"ANTHROPIC_API_KEY": "ref:mogil-anthropic"},
        "max_lifetime_minutes": 60,
    }
    policy.update(policy_overrides)
    return Configuration.model_validate(
        {
            "id": "harbor-daytona",
            "provider": "anthropic",
            "model": "claude-test",
            "adapter": "harbor",
            "backend": "harbor",
            "environment_type": "daytona",
            "environment_policy": policy,
            "harness": Harness(name="harbor", version=HARBOR_VERSION),
        }
    )


def daytona_translation(tmp_path: Path) -> Any:
    pack_path, task, _docker_config, verifier = inputs("HIDDEN_CANARY", tmp_path)
    return translate_harbor_task(
        pack_path,
        task,
        daytona_configuration(),
        create_attempt_identity("logical", attempt_id_factory=lambda: "attempt"),
        tmp_path / "translation",
        hidden_verifier=verifier,
        test_agent_import_path="agent:DeterministicTestAgent",
    )


def adapter_environment(tmp_path: Path, session_id: str) -> MogilDaytonaEnvironment:
    environment_dir = tmp_path / "environment"
    environment_dir.mkdir()
    trial_paths = TrialPaths(trial_dir=tmp_path / "trial")
    trial_paths.mkdir()
    return MogilDaytonaEnvironment(
        environment_dir=environment_dir,
        environment_name="fictional-task",
        session_id=session_id,
        trial_paths=trial_paths,
        task_env_config=HarborTaskEnvironmentConfig(
            cpus=2,
            memory_mb=4096,
            storage_mb=8192,
            docker_image=IMAGE,
            workdir="/workspace",
        ),
        mounts=[],
        attempt_id="attempt",
        max_lifetime_minutes=60,
        secrets={"ANTHROPIC_API_KEY": "mogil-anthropic"},
        network_policy=NetworkPolicy(
            network_mode=(
                HarborNetworkMode.NO_NETWORK
                if "__verifier__" in session_id
                else HarborNetworkMode.ALLOWLIST
            ),
            allowed_hosts=([] if "__verifier__" in session_id else ["api.anthropic.com"]),
        ),
    )


def daytona_probes(
    *,
    credentials: bool = True,
    importable: bool = True,
    plaintext_secret: bool = False,
) -> PreflightProbes:
    return PreflightProbes(
        python_version=lambda: (3, 12),
        harbor_version=lambda: HARBOR_VERSION,
        find_executable=lambda _name: None,
        run_command=lambda argv: subprocess.CompletedProcess(argv, 1, "", ""),
        daytona_credentials=lambda: credentials,
        daytona_importable=lambda: importable,
        plaintext_secret_present=lambda _names: plaintext_secret,
    )


def test_static_daytona_consumer_contract_uses_provider_neutral_environment() -> None:
    path = ROOT / "tests/fixtures/daytona-reviewer-contract.json"
    assert validate_evidence_artifact(path) == 1
    value = json.loads(path.read_text(encoding="utf-8"))
    assert value["reviewer"]["environment_class"] == "isolated-sandbox"
    assert "daytona" not in json.dumps(value["reviewer"]).lower()


def test_daytona_translation_uses_pinned_direct_image_and_restricted_policy(
    tmp_path: Path,
) -> None:
    translation = daytona_translation(tmp_path)
    task = tomllib.loads((translation.task_dir / "task.toml").read_text(encoding="utf-8"))

    assert not (translation.task_dir / "environment/Dockerfile").exists()
    assert not (translation.task_dir / "tests/Dockerfile").exists()
    assert task["environment"] == {
        "build_timeout_sec": 600.0,
        "cpus": 2,
        "memory_mb": 4096,
        "storage_mb": 8192,
        "network_mode": "allowlist",
        "allowed_hosts": ["api.anthropic.com"],
        "docker_image": IMAGE,
        "workdir": "/workspace",
    }
    assert task["verifier"]["network_mode"] == "no-network"
    assert task["verifier"]["environment"]["docker_image"] == IMAGE
    assert translation.environment_config["import_path"].endswith(":MogilDaytonaEnvironment")
    assert translation.environment_config["cpu_enforcement_policy"] == "request"
    assert translation.environment_config["memory_enforcement_policy"] == "request"
    assert translation.environment_config["kwargs"] == {
        "secrets": {"ANTHROPIC_API_KEY": "mogil-anthropic"},
        "attempt_id": "attempt",
        "max_lifetime_minutes": 60,
    }


def test_harbor_daytona_enforces_network_policy_in_provider_create_parameters(
    tmp_path: Path,
) -> None:
    environment_dir = tmp_path / "environment"
    environment_dir.mkdir()
    trial_paths = TrialPaths(trial_dir=tmp_path / "trial")
    trial_paths.mkdir()
    common: dict[str, Any] = {
        "environment_dir": environment_dir,
        "environment_name": "fictional-task",
        "trial_paths": trial_paths,
        "task_env_config": HarborTaskEnvironmentConfig(
            cpus=2,
            memory_mb=4096,
            storage_mb=8192,
            docker_image=IMAGE,
            workdir="/workspace",
        ),
        "mounts": [],
        "attempt_id": "attempt",
        "max_lifetime_minutes": 60,
        "secrets": {"ANTHROPIC_API_KEY": "mogil-anthropic"},
    }
    agent = MogilDaytonaEnvironment(
        **common,
        session_id="trial__env",
        network_policy=NetworkPolicy(
            network_mode=HarborNetworkMode.ALLOWLIST,
            allowed_hosts=["api.anthropic.com"],
        ),
    )
    capabilities = agent.capabilities
    assert capabilities.disable_internet is True
    assert capabilities.network_allowlist is True
    assert agent._create_network_kwargs() == {
        "network_block_all": False,
        "domain_allow_list": "api.anthropic.com",
    }
    provider_state = SimpleNamespace(
        cpu=2,
        memory=4,
        disk=8,
        network_block_all=False,
        domain_allow_list="api.anthropic.com",
        network_allow_list=None,
        labels={"mogil.attempt": "attempt"},
    )
    assert agent._provider_effective_policy(provider_state) == {
        "cpus": 2,
        "memory_mb": 4096,
        "storage_mb": 8192,
        "network_mode": "allowlist",
        "allowed_hosts": ["api.anthropic.com"],
        "attempt_label_verified": True,
        "runtime_prerequisites_verified": True,
    }

    async def refresh_data() -> None:
        return None

    provider_state.id = "sandbox-agent"
    provider_state.refresh_data = refresh_data

    async def successful_runtime_exec(*_args: Any, **_kwargs: Any) -> ExecResult:
        return ExecResult(stdout="", stderr="", return_code=0)

    agent._sandbox_exec = successful_runtime_exec  # type: ignore[method-assign]
    asyncio.run(agent._verify_runtime_prerequisites())

    async def failed_runtime_exec(*_args: Any, **_kwargs: Any) -> ExecResult:
        return ExecResult(stdout="", stderr="missing python", return_code=1)

    agent._sandbox_exec = failed_runtime_exec  # type: ignore[method-assign]
    with pytest.raises(RuntimeError, match="verifier runtime prerequisites"):
        asyncio.run(agent._verify_runtime_prerequisites())

    async def runtime_prerequisites() -> None:
        return None

    agent._verify_runtime_prerequisites = runtime_prerequisites  # type: ignore[method-assign]

    class ProviderDouble:
        def __init__(self, state: Any) -> None:
            self.state = state

        async def create(self, *, params: Any, timeout: int) -> Any:
            assert timeout == 600
            assert params.labels["mogil.attempt"] == "attempt"
            assert params.labels["mogil.managed"] == "true"
            assert params.labels["harbor.managed"] == "true"
            assert datetime.fromisoformat(params.labels["mogil.expires_at"]).tzinfo
            return self.state

    create_params = SimpleNamespace(secrets={"ANTHROPIC_API_KEY": "mogil-anthropic"}, labels=None)
    missing_state = SimpleNamespace(
        id="sandbox-missing",
        cpu=2,
        memory=4,
        network_block_all=False,
        domain_allow_list="api.anthropic.com",
        network_allow_list=None,
        labels={"mogil.attempt": "attempt"},
        refresh_data=refresh_data,
    )
    asyncio.run(agent._create_sandbox(create_params, ProviderDouble(missing_state)))
    receipt_path = next((trial_paths.trial_dir / "mogil-daytona-policy").glob("*.json"))
    missing_receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    assert missing_receipt["status"] == "unverified"
    assert missing_receipt["effective"] is None

    asyncio.run(agent._create_sandbox(create_params, ProviderDouble(provider_state)))
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    assert receipt["source"] == "daytona_provider_refresh"
    assert receipt["sandbox_id"] == "sandbox-agent"
    assert receipt["effective"]["storage_mb"] == 8192
    assert receipt["create_parameters"] == {"secret_references_attached": True}
    assert (
        agent._provider_effective_policy(
            SimpleNamespace(
                cpu=2,
                memory=4,
                network_block_all=False,
                domain_allow_list="api.anthropic.com",
                network_allow_list=None,
                labels={"mogil.attempt": "attempt"},
            )
        )
        is None
    )

    verifier = MogilDaytonaEnvironment(
        **common,
        session_id="trial__verifier__trial",
        network_policy=NetworkPolicy(network_mode=HarborNetworkMode.NO_NETWORK),
    )
    assert verifier._create_network_kwargs() == {"network_block_all": True}
    assert verifier._secrets is None
    verifier._verify_runtime_prerequisites = runtime_prerequisites  # type: ignore[method-assign]
    verifier_state = SimpleNamespace(
        id="sandbox-verifier",
        cpu=2,
        memory=4,
        disk=8,
        network_block_all=True,
        domain_allow_list=None,
        network_allow_list=None,
        labels={"mogil.attempt": "attempt"},
        refresh_data=refresh_data,
    )
    verifier_params = SimpleNamespace(secrets=None, labels=None)
    asyncio.run(verifier._create_sandbox(verifier_params, ProviderDouble(verifier_state)))
    verifier_receipt_path = next(
        (trial_paths.trial_dir / "mogil-daytona-policy").glob("*verifier*.json")
    )
    verifier_receipt = json.loads(verifier_receipt_path.read_text(encoding="utf-8"))
    assert verifier_receipt["effective"]["network_mode"] == "no-network"
    assert verifier_receipt["create_parameters"] == {"secret_references_attached": False}


def test_adapter_already_absent_deletion_is_confirmed(tmp_path: Path) -> None:
    environment = adapter_environment(tmp_path, "trial__env")

    class AbsentSandbox:
        id = "sandbox-absent"

        async def delete(self) -> None:
            raise DaytonaNotFoundError

    environment._sandbox = AbsentSandbox()  # type: ignore[assignment]
    asyncio.run(environment._stop_sandbox())

    receipt_path = next(
        (environment.trial_paths.trial_dir / "mogil-daytona-cleanup").glob("*.json")
    )
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    assert receipt["status"] == "confirmed"
    assert receipt["sandbox_id"] == "sandbox-absent"


def test_adapter_uses_bounded_exponential_confirmation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    environment = adapter_environment(tmp_path, "trial__env")
    assert harbor_daytona._DELETION_CONFIRMATION_DELAYS_SECONDS == (
        0.25,
        0.5,
        1.0,
        2.0,
        4.0,
        8.0,
    )

    class Sandbox:
        id = "sandbox-eventual"

        async def delete(self) -> None:
            return None

    class Client:
        calls = 0

        async def get(self, _sandbox_id: str) -> object:
            self.calls += 1
            if self.calls > 1:
                raise DaytonaNotFoundError
            return object()

    class Manager:
        async def get_client(self) -> Client:
            return client

    delays: list[float] = []

    async def fake_sleep(delay: float) -> None:
        delays.append(delay)

    client = Client()
    environment._sandbox = Sandbox()  # type: ignore[assignment]
    environment._client_manager = Manager()  # type: ignore[assignment]
    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    asyncio.run(environment._stop_sandbox())

    assert delays == [0.25]


def test_adapter_failed_deletion_confirmation_is_recorded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    environment = adapter_environment(tmp_path, "trial__env")

    class Sandbox:
        id = "sandbox-still-present"

        async def delete(self) -> None:
            return None

    class Client:
        async def get(self, _sandbox_id: str) -> object:
            return object()

    class Manager:
        async def get_client(self) -> Client:
            return Client()

    environment._sandbox = Sandbox()  # type: ignore[assignment]
    environment._client_manager = Manager()  # type: ignore[assignment]
    monkeypatch.setattr(harbor_daytona, "_DELETION_CONFIRMATION_DELAYS_SECONDS", (0.0, 0.0))
    with pytest.raises(RuntimeError, match="remains after deletion"):
        asyncio.run(environment._stop_sandbox())

    receipt_path = next(
        (environment.trial_paths.trial_dir / "mogil-daytona-cleanup").glob("*.json")
    )
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    assert receipt["status"] == "failed"


def test_daytona_policy_fails_closed_on_weak_or_unsafe_values() -> None:
    invalid = [
        {"image": "ghcr.io/mogil/latest"},
        {"memory_mb": 512},
        {"storage_mb": 1024},
        {"network_mode": "no-network", "allowed_hosts": []},
        {"allowed_hosts": []},
        {"secret_refs": {}},
        {"secret_refs": {"ANTHROPIC_API_KEY": "plaintext-secret-value"}},
    ]
    for override in invalid:
        with pytest.raises(ValidationError):
            daytona_configuration(**override)


def test_daytona_preflight_is_credential_gated_before_output(tmp_path: Path) -> None:
    translation = daytona_translation(tmp_path)
    output = tmp_path / "output"
    with pytest.raises(PreflightError, match="credentials unavailable"):
        preflight(output, translation, probes=daytona_probes(credentials=False))
    assert not output.exists()
    with pytest.raises(PreflightError, match="not installed"):
        preflight(output, translation, probes=daytona_probes(importable=False))
    assert not output.exists()
    with pytest.raises(PreflightError, match="plaintext model credentials"):
        preflight(output, translation, probes=daytona_probes(plaintext_secret=True))
    assert not output.exists()
    preflight(output, translation, probes=daytona_probes())


class ConfirmedDaytonaJob(RecordingJob):
    async def run(self) -> Any:
        result = await super().run()
        trial_name = result.trial_results[0].trial_name
        trial = self.job_dir / trial_name
        (trial / "lock.json").write_text(
            json.dumps(
                {"environment": self.config.environment.model_dump(mode="json", exclude_none=True)}
            ),
            encoding="utf-8",
        )
        cleanup_receipts = trial / "mogil-daytona-cleanup"
        policy_receipts = trial / "mogil-daytona-policy"
        cleanup_receipts.mkdir()
        policy_receipts.mkdir()
        sessions = {
            "agent": f"{trial_name}__env",
            "verifier": f"{trial_name}__verifier__trial",
        }
        for role, name in sessions.items():
            sandbox_id = f"sandbox-{name}"
            (cleanup_receipts / f"{name}.json").write_text(
                json.dumps(
                    {
                        "attempt_id": "attempt",
                        "session_id": name,
                        "sandbox_id": sandbox_id,
                        "status": "confirmed",
                        "error": None,
                    }
                ),
                encoding="utf-8",
            )
            effective = {
                "cpus": 2,
                "memory_mb": 4096,
                "storage_mb": 8192,
                "network_mode": "allowlist" if role == "agent" else "no-network",
                "allowed_hosts": ["api.anthropic.com"] if role == "agent" else [],
                "attempt_label_verified": True,
                "runtime_prerequisites_verified": True,
            }
            (policy_receipts / f"{name}.json").write_text(
                json.dumps(
                    {
                        "version": "1",
                        "attempt_id": "attempt",
                        "session_id": name,
                        "sandbox_id": sandbox_id,
                        "role": role,
                        "source": "daytona_provider_refresh",
                        "effective": effective,
                        "create_parameters": {"secret_references_attached": role == "agent"},
                        "status": "verified",
                        "error": None,
                    }
                ),
                encoding="utf-8",
            )
        return result


def test_daytona_fake_satisfies_same_bundle_and_cleanup_contract(tmp_path: Path) -> None:
    translation = daytona_translation(tmp_path)

    async def factory(config: Any) -> ConfirmedDaytonaJob:
        return ConfirmedDaytonaJob(config)

    result = asyncio.run(
        HarborBackend(job_factory=factory).run_attempt(
            translation,
            create_attempt_identity("logical", attempt_id_factory=lambda: "attempt"),
            tmp_path / "results",
            _fixture_profile=_FIXTURE_PROFILE_TOKEN,
        )
    )

    assert result.evidence_status == EvidenceStatus.FIXTURE_COMPLETE
    assert result.cleanup["status"] == "confirmed"
    assert len(result.cleanup["deletion_receipts"]) == 2
    environment = json.loads((result.bundle_dir / "environment.json").read_text(encoding="utf-8"))
    assert environment["class"] == "isolated-sandbox"
    assert environment["provider"] == "daytona"
    assert environment["effective"] == {
        "source": "provider-reported",
        "agent": {
            "cpus": 2,
            "memory_mb": 4096,
            "storage_mb": 8192,
            "network_mode": "allowlist",
            "allowed_hosts": ["api.anthropic.com"],
            "attempt_label_verified": True,
            "runtime_prerequisites_verified": True,
        },
        "verifier": {
            "cpus": 2,
            "memory_mb": 4096,
            "storage_mb": 8192,
            "network_mode": "no-network",
            "allowed_hosts": [],
            "attempt_label_verified": True,
            "runtime_prerequisites_verified": True,
        },
    }
    assert environment["effective"] != environment["requested"]
    assert (result.bundle_dir / "environment/provider-policy-agent.json").is_file()
    assert (result.bundle_dir / "environment/provider-policy-verifier.json").is_file()


def test_harbor_lock_is_not_relabelled_as_provider_effective_state(
    tmp_path: Path,
) -> None:
    translation = daytona_translation(tmp_path)

    class WeakSerializedLockJob(ConfirmedDaytonaJob):
        async def run(self) -> Any:
            result = await super().run()
            trial = self.job_dir / result.trial_results[0].trial_name
            lock = json.loads((trial / "lock.json").read_text(encoding="utf-8"))
            lock["environment"]["memory_enforcement_policy"] = "ignore"
            (trial / "lock.json").write_text(json.dumps(lock), encoding="utf-8")
            return result

    async def factory(config: Any) -> WeakSerializedLockJob:
        return WeakSerializedLockJob(config)

    result = asyncio.run(
        HarborBackend(job_factory=factory).run_attempt(
            translation,
            create_attempt_identity("logical", attempt_id_factory=lambda: "attempt"),
            tmp_path / "results",
            _fixture_profile=_FIXTURE_PROFILE_TOKEN,
        )
    )

    assert result.evidence_status == EvidenceStatus.FIXTURE_COMPLETE
    environment = json.loads((result.bundle_dir / "environment.json").read_text(encoding="utf-8"))
    assert environment["effective"]["source"] == "provider-reported"
    assert environment["effective"] != environment["requested"]


def test_daytona_effective_policy_degradation_fails_closed(tmp_path: Path) -> None:
    translation = daytona_translation(tmp_path)

    class WeakPolicyJob(ConfirmedDaytonaJob):
        async def run(self) -> Any:
            result = await super().run()
            trial = self.job_dir / result.trial_results[0].trial_name
            receipt_path = next((trial / "mogil-daytona-policy").glob("*__env.json"))
            receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
            receipt["effective"]["memory_mb"] = 1024
            receipt_path.write_text(json.dumps(receipt), encoding="utf-8")
            return result

    async def factory(config: Any) -> WeakPolicyJob:
        return WeakPolicyJob(config)

    result = asyncio.run(
        HarborBackend(job_factory=factory).run_attempt(
            translation,
            create_attempt_identity("logical", attempt_id_factory=lambda: "attempt"),
            tmp_path / "results",
            _fixture_profile=_FIXTURE_PROFILE_TOKEN,
        )
    )

    assert result.run["infrastructure_outcome"] == "failed"
    assert result.evidence_status == EvidenceStatus.INSUFFICIENT
    environment = json.loads((result.bundle_dir / "environment.json").read_text(encoding="utf-8"))
    assert environment["effective"] is None
    assert list((result.bundle_dir / "environment").glob("provider-policy-unverified-*.json"))


def test_daytona_missing_provider_field_fails_closed(tmp_path: Path) -> None:
    translation = daytona_translation(tmp_path)

    class MissingDiskJob(ConfirmedDaytonaJob):
        async def run(self) -> Any:
            result = await super().run()
            trial = self.job_dir / result.trial_results[0].trial_name
            receipt_path = next((trial / "mogil-daytona-policy").glob("*__env.json"))
            receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
            del receipt["effective"]["storage_mb"]
            receipt_path.write_text(json.dumps(receipt), encoding="utf-8")
            return result

    async def factory(config: Any) -> MissingDiskJob:
        return MissingDiskJob(config)

    result = asyncio.run(
        HarborBackend(job_factory=factory).run_attempt(
            translation,
            create_attempt_identity("logical", attempt_id_factory=lambda: "attempt"),
            tmp_path / "results",
            _fixture_profile=_FIXTURE_PROFILE_TOKEN,
        )
    )

    assert result.run["infrastructure_outcome"] == "failed"
    assert result.evidence_status == EvidenceStatus.INSUFFICIENT
    environment = json.loads((result.bundle_dir / "environment.json").read_text(encoding="utf-8"))
    assert environment["effective"] is None


class DaytonaNotFoundError(Exception):
    pass


@pytest.mark.parametrize(
    "problem",
    ["wrong-session", "extra-policy", "extra-cleanup", "sandbox-mismatch"],
)
def test_daytona_rejects_unbound_or_extra_receipts(tmp_path: Path, problem: str) -> None:
    translation = daytona_translation(tmp_path)

    class InvalidReceiptJob(ConfirmedDaytonaJob):
        async def run(self) -> Any:
            result = await super().run()
            trial = self.job_dir / result.trial_results[0].trial_name
            policy_dir = trial / "mogil-daytona-policy"
            cleanup_dir = trial / "mogil-daytona-cleanup"
            agent_policy_path = next(policy_dir.glob("*__env.json"))
            if problem == "extra-policy":
                (policy_dir / "extra.json").write_bytes(agent_policy_path.read_bytes())
            elif problem == "extra-cleanup":
                agent_cleanup_path = next(cleanup_dir.glob("*__env.json"))
                (cleanup_dir / "extra.json").write_bytes(agent_cleanup_path.read_bytes())
            else:
                receipt = json.loads(agent_policy_path.read_text(encoding="utf-8"))
                if problem == "wrong-session":
                    receipt["session_id"] = "wrong-session"
                else:
                    receipt["sandbox_id"] = "different-sandbox"
                agent_policy_path.write_text(json.dumps(receipt), encoding="utf-8")
            return result

    async def factory(config: Any) -> InvalidReceiptJob:
        return InvalidReceiptJob(config)

    result = asyncio.run(
        HarborBackend(job_factory=factory).run_attempt(
            translation,
            create_attempt_identity("logical", attempt_id_factory=lambda: "attempt"),
            tmp_path / "results",
            _fixture_profile=_FIXTURE_PROFILE_TOKEN,
        )
    )

    assert result.evidence_status == EvidenceStatus.INSUFFICIENT
    assert result.run["infrastructure_outcome"] == "failed"


class FakeReaperClient:
    def __init__(self, sandboxes: list[ManagedSandbox]) -> None:
        self.listing = list(sandboxes)
        self.sandboxes = {sandbox.id: sandbox for sandbox in sandboxes}
        self.deleted: list[str] = []
        self.concurrently_deleted: set[str] = set()
        self.not_found_on_confirm: set[str] = set()
        self.list_calls = 0

    async def list_managed(self, *, limit: int):  # type: ignore[no-untyped-def]
        values = self.listing if self.list_calls == 0 else list(self.sandboxes.values())
        self.list_calls += 1
        for sandbox in values[:limit]:
            yield sandbox

    async def fetch(self, sandbox_id: str) -> ManagedSandbox | None:
        return self.sandboxes.get(sandbox_id)

    async def delete(self, sandbox_id: str) -> None:
        self.deleted.append(sandbox_id)
        if sandbox_id in self.concurrently_deleted:
            self.sandboxes.pop(sandbox_id, None)
            raise DaytonaNotFoundError
        self.sandboxes.pop(sandbox_id)

    async def is_absent(self, sandbox_id: str) -> bool:
        if sandbox_id in self.not_found_on_confirm:
            raise DaytonaNotFoundError
        return sandbox_id not in self.sandboxes


def test_reaper_is_expiry_scoped_bounded_and_confirms_deletion() -> None:
    now = datetime(2026, 7, 11, tzinfo=UTC)
    expired = (now - timedelta(minutes=1)).isoformat()
    future = (now + timedelta(minutes=1)).isoformat()
    managed = {"harbor.managed": "true", "mogil.managed": "true"}
    client = FakeReaperClient(
        [
            ManagedSandbox("old-1", {**managed, "mogil.expires_at": expired}),
            ManagedSandbox("old-2", {**managed, "mogil.expires_at": expired}),
            ManagedSandbox("active", {**managed, "mogil.expires_at": future}),
            ManagedSandbox("unlabeled", {"mogil.expires_at": expired}),
        ]
    )

    result = asyncio.run(reap_expired(client, now=now, delete_limit=1, scan_limit=4))

    assert client.deleted == ["old-1"]
    assert result.scanned == 4
    assert result.expired == 2
    assert result.deleted == 1
    assert set(client.sandboxes) == {"old-2", "active", "unlabeled"}
    with pytest.raises(ValueError, match="out of bounds"):
        asyncio.run(reap_expired(client, now=now, delete_limit=101))


def test_reaper_treats_already_absent_as_confirmed() -> None:
    now = datetime(2026, 7, 11, tzinfo=UTC)
    managed = {
        "harbor.managed": "true",
        "mogil.managed": "true",
        "mogil.expires_at": (now - timedelta(minutes=1)).isoformat(),
    }
    client = FakeReaperClient([ManagedSandbox("gone", managed)])
    client.sandboxes.clear()

    result = asyncio.run(reap_expired(client, now=now))

    assert result.deleted == 1
    assert client.deleted == []


def test_reaper_treats_not_found_during_confirmation_as_success() -> None:
    now = datetime(2026, 7, 11, tzinfo=UTC)
    managed = {
        "harbor.managed": "true",
        "mogil.managed": "true",
        "mogil.expires_at": (now - timedelta(minutes=1)).isoformat(),
    }
    client = FakeReaperClient([ManagedSandbox("confirm-race", managed)])
    client.not_found_on_confirm.add("confirm-race")

    result = asyncio.run(reap_expired(client, now=now))

    assert result.deleted == 1


def test_reaper_treats_concurrent_delete_as_confirmed() -> None:
    now = datetime(2026, 7, 11, tzinfo=UTC)
    managed = {
        "harbor.managed": "true",
        "mogil.managed": "true",
        "mogil.expires_at": (now - timedelta(minutes=1)).isoformat(),
    }
    client = FakeReaperClient([ManagedSandbox("concurrent", managed)])
    client.concurrently_deleted.add("concurrent")

    result = asyncio.run(reap_expired(client, now=now))

    assert result.deleted == 1
    assert client.deleted == ["concurrent"]


@pytest.mark.parametrize("change", ["label", "expiry"])
def test_reaper_revalidates_fresh_labels_and_expiry(change: str) -> None:
    now = datetime(2026, 7, 11, tzinfo=UTC)
    expired_labels = {
        "harbor.managed": "true",
        "mogil.managed": "true",
        "mogil.expires_at": (now - timedelta(minutes=1)).isoformat(),
    }
    client = FakeReaperClient([ManagedSandbox("changed", expired_labels)])
    fresh_labels = dict(expired_labels)
    if change == "label":
        fresh_labels.pop("mogil.managed")
    else:
        fresh_labels["mogil.expires_at"] = (now + timedelta(minutes=1)).isoformat()
    client.sandboxes["changed"] = ManagedSandbox("changed", fresh_labels)

    result = asyncio.run(reap_expired(client, now=now))

    assert result.expired == 1
    assert result.deleted == 0
    assert client.deleted == []
    assert "changed" in client.sandboxes
