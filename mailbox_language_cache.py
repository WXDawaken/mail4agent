from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from mailbox_language_runtime import compile_protocol_runtime_schema, format_protocol_ref


PROTOCOL_RUNTIME_CACHE_KIND = "mailbox_language_protocol_runtime_cache_entry"
PROTOCOL_RUNTIME_CACHE_SCHEMA_VERSION = 1
PROTOCOL_RUNTIME_COMPILER_VERSION = "protocol-runtime-v1"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


@dataclass(frozen=True)
class ProtocolRuntimeCacheResult:
    artifact: dict[str, Any]
    cache_hit: bool
    cache_path: Path
    source_sha256: str


class ProtocolRuntimeDiskCache:
    def __init__(self, cache_dir: str | Path):
        self.cache_dir = Path(cache_dir)

    def load_or_compile(
        self,
        *,
        protocol_name: str,
        protocol_version: str,
        schema: dict[str, Any],
    ) -> ProtocolRuntimeCacheResult:
        protocol_ref = format_protocol_ref(protocol_name, protocol_version)
        source_sha256 = self._source_sha256(
            protocol_name=protocol_name,
            protocol_version=protocol_version,
            schema=schema,
        )
        cache_path = self._cache_path(protocol_ref=protocol_ref, source_sha256=source_sha256)
        cached_entry = self._load_cache_entry(cache_path)
        if cached_entry is not None:
            artifact = cached_entry.get("artifact")
            if isinstance(artifact, dict):
                return ProtocolRuntimeCacheResult(
                    artifact=artifact,
                    cache_hit=True,
                    cache_path=cache_path,
                    source_sha256=source_sha256,
                )

        artifact = compile_protocol_runtime_schema(
            schema,
            protocol_name=protocol_name,
            protocol_version=protocol_version,
        )
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_text(
            json.dumps(
                {
                    "kind": PROTOCOL_RUNTIME_CACHE_KIND,
                    "cache_schema_version": PROTOCOL_RUNTIME_CACHE_SCHEMA_VERSION,
                    "compiler_version": PROTOCOL_RUNTIME_COMPILER_VERSION,
                    "protocol": protocol_ref,
                    "source_sha256": source_sha256,
                    "compiled_at": utc_now(),
                    "artifact": artifact,
                },
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            ),
            encoding="utf-8",
        )
        return ProtocolRuntimeCacheResult(
            artifact=artifact,
            cache_hit=False,
            cache_path=cache_path,
            source_sha256=source_sha256,
        )

    def _cache_path(self, *, protocol_ref: str, source_sha256: str) -> Path:
        protocol_dir_name = protocol_ref.replace("/", "__")
        return self.cache_dir / "protocol-runtime" / protocol_dir_name / f"{source_sha256}.json"

    def _load_cache_entry(self, cache_path: Path) -> dict[str, Any] | None:
        if not cache_path.exists():
            return None
        try:
            payload = json.loads(cache_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return None
        if not isinstance(payload, dict):
            return None
        if payload.get("kind") != PROTOCOL_RUNTIME_CACHE_KIND:
            return None
        if payload.get("cache_schema_version") != PROTOCOL_RUNTIME_CACHE_SCHEMA_VERSION:
            return None
        if payload.get("compiler_version") != PROTOCOL_RUNTIME_COMPILER_VERSION:
            return None
        return payload

    def _source_sha256(
        self,
        *,
        protocol_name: str,
        protocol_version: str,
        schema: dict[str, Any],
    ) -> str:
        canonical = json.dumps(
            {
                "compiler_version": PROTOCOL_RUNTIME_COMPILER_VERSION,
                "protocol": format_protocol_ref(protocol_name, protocol_version),
                "schema": schema,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(canonical).hexdigest()
