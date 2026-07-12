from __future__ import annotations

import asyncio
import json
import math
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol, override

from harbor.environments.daytona import (  # type: ignore[import-untyped]
    DaytonaClientManager,
    DaytonaEnvironment,
)

CLEANUP_RECEIPT_DIRECTORY = "mogil-daytona-cleanup"
POLICY_RECEIPT_DIRECTORY = "mogil-daytona-policy"
HARBOR_MANAGED_LABEL = "harbor.managed"
MANAGED_LABEL = "mogil.managed"
ATTEMPT_LABEL = "mogil.attempt"
EXPIRES_LABEL = "mogil.expires_at"
_DELETION_CONFIRMATION_DELAYS_SECONDS = (0.25, 0.5, 1.0, 2.0, 4.0, 8.0)


def _is_not_found(error: BaseException) -> bool:
    return type(error).__name__ == "DaytonaNotFoundError"


class MogilDaytonaEnvironment(DaytonaEnvironment):  # type: ignore[misc]
    """Harbor Daytona adapter that adds bounded labels and deletion confirmation."""

    def __init__(
        self,
        *args: Any,
        attempt_id: str,
        max_lifetime_minutes: int,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
        **kwargs: Any,
    ) -> None:
        session_id = kwargs.get("session_id")
        self._mogil_is_verifier = bool(
            isinstance(session_id, str) and "__verifier__" in session_id
        )
        if self._mogil_is_verifier:
            # Harbor creates the verifier through the same environment config. Never
            # attach model credentials to its separately network-disabled sandbox.
            kwargs["secrets"] = None
        self._mogil_attempt_id = attempt_id
        self._mogil_expires_at = now() + timedelta(minutes=max_lifetime_minutes)
        super().__init__(*args, **kwargs)

    @override
    def _sandbox_labels(self) -> dict[str, str]:
        return {
            **super()._sandbox_labels(),
            MANAGED_LABEL: "true",
            ATTEMPT_LABEL: self._mogil_attempt_id,
            EXPIRES_LABEL: self._mogil_expires_at.isoformat(),
        }

    async def _create_sandbox(self, params: Any, daytona: Any = None) -> None:
        """Create through Harbor, then refresh provider-returned effective state."""
        await super()._create_sandbox(params=params, daytona=daytona)
        sandbox = self._sandbox
        if sandbox is None:
            self._write_policy_receipt(
                sandbox_id=None,
                effective=None,
                secret_references_attached=bool(getattr(params, "secrets", None)),
                error="sandbox reference unavailable after creation",
            )
            return
        sandbox_id = str(sandbox.id)
        try:
            await sandbox.refresh_data()
            await self._verify_runtime_prerequisites()
            effective = self._provider_effective_policy(sandbox)
            self._write_policy_receipt(
                sandbox_id=sandbox_id,
                effective=effective,
                secret_references_attached=bool(getattr(params, "secrets", None)),
                error=None if effective is not None else "provider fields unavailable",
            )
        except Exception as error:
            self._write_policy_receipt(
                sandbox_id=sandbox_id,
                effective=None,
                secret_references_attached=bool(getattr(params, "secrets", None)),
                error=type(error).__name__,
            )

    async def _verify_runtime_prerequisites(self) -> None:
        result = await self._sandbox_exec(
            "test -x /usr/local/bin/python && "
            "/usr/local/bin/python -c 'import sys; "
            "raise SystemExit(0 if sys.version_info[:2] == (3, 12) else 1)' && "
            "test -x /bin/sh",
            shell="sh -c",
        )
        if result.return_code != 0:
            raise RuntimeError("pinned sandbox image lacks verifier runtime prerequisites")

    def _provider_effective_policy(self, sandbox: Any) -> dict[str, object] | None:
        cpu = getattr(sandbox, "cpu", None)
        memory_gib = getattr(sandbox, "memory", None)
        disk_gib = getattr(sandbox, "disk", None)
        block_all = getattr(sandbox, "network_block_all", None)
        domain_allow = getattr(sandbox, "domain_allow_list", None)
        network_allow = getattr(sandbox, "network_allow_list", None)
        labels = getattr(sandbox, "labels", None)
        if (
            not isinstance(cpu, (int, float))
            or isinstance(cpu, bool)
            or not isinstance(memory_gib, (int, float))
            or isinstance(memory_gib, bool)
            or not isinstance(disk_gib, (int, float))
            or isinstance(disk_gib, bool)
            or not isinstance(block_all, bool)
            or not isinstance(labels, dict)
        ):
            return None
        if (
            not math.isfinite(float(cpu))
            or not math.isfinite(float(memory_gib))
            or not math.isfinite(float(disk_gib))
            or cpu <= 0
            or memory_gib <= 0
            or disk_gib <= 0
        ):
            return None
        memory_mb = memory_gib * 1024
        storage_mb = disk_gib * 1024
        if not float(memory_mb).is_integer() or not float(storage_mb).is_integer():
            return None
        allowed_hosts: list[str] = []
        if block_all:
            network_mode = "no-network"
        elif isinstance(domain_allow, str) and domain_allow:
            network_mode = "allowlist"
            allowed_hosts = sorted(value for value in domain_allow.split(",") if value)
        elif isinstance(network_allow, str) and network_allow:
            network_mode = "allowlist"
            allowed_hosts = sorted(value for value in network_allow.split(",") if value)
        else:
            network_mode = "public"
        if len(allowed_hosts) > 64 or any(len(host) > 253 for host in allowed_hosts):
            return None
        return {
            "cpus": int(cpu) if float(cpu).is_integer() else cpu,
            "memory_mb": int(memory_mb),
            "storage_mb": int(storage_mb),
            "network_mode": network_mode,
            "allowed_hosts": allowed_hosts,
            "attempt_label_verified": labels.get(ATTEMPT_LABEL)
            == self._mogil_attempt_id,
            "runtime_prerequisites_verified": True,
        }

    def _write_policy_receipt(
        self,
        *,
        sandbox_id: str | None,
        effective: dict[str, object] | None,
        secret_references_attached: bool,
        error: str | None,
    ) -> None:
        value = {
            "version": "1",
            "attempt_id": self._mogil_attempt_id,
            "session_id": self.session_id,
            "sandbox_id": sandbox_id,
            "role": "verifier" if self._mogil_is_verifier else "agent",
            "source": "daytona_provider_refresh",
            "effective": effective,
            "create_parameters": {
                "secret_references_attached": secret_references_attached,
            },
            "status": "verified" if effective is not None and error is None else "unverified",
            "error": error,
        }
        self._write_receipt(POLICY_RECEIPT_DIRECTORY, value)

    async def _stop_sandbox(self) -> None:
        sandbox = self._sandbox
        if sandbox is None:
            self._write_cleanup_receipt(None, "unknown", "sandbox reference unavailable")
            raise RuntimeError("Daytona sandbox reference unavailable during cleanup")
        sandbox_id = str(sandbox.id)
        try:
            try:
                await sandbox.delete()
            except Exception as error:
                if _is_not_found(error):
                    self._write_cleanup_receipt(sandbox_id, "confirmed", None)
                    return
                raise
            if self._client_manager is None:
                raise RuntimeError("Daytona client unavailable for deletion confirmation")
            client = await self._client_manager.get_client()
            confirmed = False
            for delay in (0.0, *_DELETION_CONFIRMATION_DELAYS_SECONDS):
                if delay:
                    await asyncio.sleep(delay)
                try:
                    await client.get(sandbox_id)
                except Exception as error:
                    if _is_not_found(error):
                        confirmed = True
                        break
                    raise
            if not confirmed:
                raise RuntimeError("Daytona sandbox remains after deletion request")
            self._write_cleanup_receipt(sandbox_id, "confirmed", None)
        except Exception as error:
            self._write_cleanup_receipt(sandbox_id, "failed", type(error).__name__)
            raise

    def _write_cleanup_receipt(
        self, sandbox_id: str | None, status: str, error: str | None
    ) -> None:
        value = {
            "attempt_id": self._mogil_attempt_id,
            "session_id": self.session_id,
            "sandbox_id": sandbox_id,
            "status": status,
            "error": error,
        }
        self._write_receipt(CLEANUP_RECEIPT_DIRECTORY, value)

    def _write_receipt(self, directory_name: str, value: dict[str, object]) -> None:
        directory = self.trial_paths.trial_dir / directory_name
        directory.mkdir(parents=True, exist_ok=True)
        safe_session = "".join(
            character if character.isalnum() or character in "-._" else "_"
            for character in self.session_id
        )
        path = directory / f"{safe_session}.json"
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps(value, sort_keys=True) + "\n", encoding="utf-8")
        temporary.replace(path)


@dataclass(frozen=True)
class ManagedSandbox:
    id: str
    labels: dict[str, str]


class ReaperClient(Protocol):
    def list_managed(self, *, limit: int) -> AsyncIterator[ManagedSandbox]: ...

    async def fetch(self, sandbox_id: str) -> ManagedSandbox | None: ...

    async def delete(self, sandbox_id: str) -> None: ...

    async def is_absent(self, sandbox_id: str) -> bool: ...


@dataclass(frozen=True)
class ReaperResult:
    scanned: int
    expired: int
    deleted: int
    remaining: int


def _is_expired_managed(sandbox: ManagedSandbox, now: datetime) -> bool:
    if (
        sandbox.labels.get(HARBOR_MANAGED_LABEL) != "true"
        or sandbox.labels.get(MANAGED_LABEL) != "true"
    ):
        return False
    raw = sandbox.labels.get(EXPIRES_LABEL)
    try:
        expires_at = datetime.fromisoformat(raw) if raw is not None else None
    except ValueError:
        return False
    return bool(
        expires_at is not None
        and expires_at.tzinfo is not None
        and expires_at <= now
    )


async def reap_expired(
    client: ReaperClient,
    *,
    now: datetime,
    delete_limit: int = 20,
    scan_limit: int = 100,
) -> ReaperResult:
    if not 1 <= delete_limit <= 100 or not 1 <= scan_limit <= 500:
        raise ValueError("reaper limits are out of bounds")
    candidates = [sandbox async for sandbox in client.list_managed(limit=scan_limit)]
    expired = [sandbox for sandbox in candidates if _is_expired_managed(sandbox, now)]
    confirmed_absent_ids: set[str] = set()
    for listed in expired[:delete_limit]:
        try:
            fresh = await client.fetch(listed.id)
        except Exception as error:
            if _is_not_found(error):
                confirmed_absent_ids.add(listed.id)
                continue
            raise
        if fresh is None:
            confirmed_absent_ids.add(listed.id)
            continue
        if not _is_expired_managed(fresh, now):
            continue
        try:
            await client.delete(fresh.id)
        except Exception as error:
            if _is_not_found(error):
                confirmed_absent_ids.add(fresh.id)
                continue
            raise
        try:
            absent = await client.is_absent(fresh.id)
        except Exception as error:
            if _is_not_found(error):
                absent = True
            else:
                raise
        if not absent:
            raise RuntimeError("expired sandbox deletion could not be confirmed")
        confirmed_absent_ids.add(fresh.id)
    remaining_ids = {sandbox.id async for sandbox in client.list_managed(limit=scan_limit)}
    return ReaperResult(
        scanned=len(candidates),
        expired=len(expired),
        deleted=len(confirmed_absent_ids),
        remaining=len(remaining_ids),
    )


class HarborDaytonaReaperClient:
    """Private adapter around Harbor's process-owned Daytona client."""

    async def _client(self) -> Any:
        manager = await DaytonaClientManager.get_instance()
        return await manager.get_client()

    async def list_managed(self, *, limit: int) -> AsyncIterator[ManagedSandbox]:
        from daytona import ListSandboxesQuery

        client = await self._client()
        query = ListSandboxesQuery(
            labels={HARBOR_MANAGED_LABEL: "true", MANAGED_LABEL: "true"},
            limit=limit,
        )
        count = 0
        async for sandbox in client.list(query):
            if count >= limit:
                break
            yield ManagedSandbox(id=str(sandbox.id), labels=dict(sandbox.labels))
            count += 1

    async def fetch(self, sandbox_id: str) -> ManagedSandbox | None:
        client = await self._client()
        try:
            sandbox = await client.get(sandbox_id)
        except Exception as error:
            if _is_not_found(error):
                return None
            raise
        return ManagedSandbox(id=str(sandbox.id), labels=dict(sandbox.labels))

    async def delete(self, sandbox_id: str) -> None:
        client = await self._client()
        try:
            sandbox = await client.get(sandbox_id)
            await sandbox.delete()
        except Exception as error:
            if _is_not_found(error):
                return
            raise

    async def is_absent(self, sandbox_id: str) -> bool:
        client = await self._client()
        try:
            await client.get(sandbox_id)
        except Exception as error:
            if _is_not_found(error):
                return True
            raise
        return False
