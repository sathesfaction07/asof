"""Settings.

No list-typed fields. Pydantic Settings parses those as JSON, so `CC_FRAMEWORKS=US,EU`
produces a decode error that reads like a bug in the tool rather than a mistake in the
environment.
"""

from __future__ import annotations

from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="CC_", env_file=".env", extra="ignore")

    #: Directory of per-framework YAML rule files.
    rules_dir: Path = Path("data/frameworks")

    #: Default framework when one is not given on the command line.
    framework: str = "US"

    #: Append-only JSON Lines audit log.
    audit_log: Path = Path("audit/checks.jsonl")

    output_dir: Path = Path("output")
    output_format: str = "csv"
    log_level: str = "INFO"
