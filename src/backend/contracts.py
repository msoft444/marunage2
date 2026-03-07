import hashlib
import json
from dataclasses import dataclass
from typing import Any


class ContractValidationError(ValueError):
    pass


@dataclass(frozen=True)
class ModelContract:
    phase: int
    model: str
    cli_profile: str
    fallback_allowed: bool
    contract_version: str
    allowed_aliases: tuple[str, ...] = ()


class ModelContractCodec:
    REQUIRED_FIELDS = (
        "phase",
        "model",
        "cli_profile",
        "fallback_allowed",
        "contract_version",
    )

    @classmethod
    def canonicalize(cls, payload: dict[str, Any]) -> str:
        filtered = {key: payload[key] for key in cls.REQUIRED_FIELDS}
        return json.dumps(filtered, sort_keys=True, separators=(",", ":"), ensure_ascii=False)

    @classmethod
    def digest(cls, payload: dict[str, Any]) -> str:
        canonical = cls.canonicalize(payload)
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    @classmethod
    def parse(cls, raw_contract: str) -> ModelContract:
        try:
            payload = json.loads(raw_contract)
        except json.JSONDecodeError as exc:
            raise ContractValidationError("invalid json") from exc

        missing = [field for field in cls.REQUIRED_FIELDS if field not in payload]
        if missing:
            raise ContractValidationError(f"missing required fields: {', '.join(missing)}")

        if not isinstance(payload["phase"], int):
            raise ContractValidationError("phase must be int")
        if not isinstance(payload["model"], str):
            raise ContractValidationError("model must be str")
        if not isinstance(payload["cli_profile"], str):
            raise ContractValidationError("cli_profile must be str")
        if not isinstance(payload["fallback_allowed"], bool):
            raise ContractValidationError("fallback_allowed must be bool")
        if not isinstance(payload["contract_version"], str):
            raise ContractValidationError("contract_version must be str")

        aliases = payload.get("allowed_aliases", ())
        if not isinstance(aliases, (list, tuple)):
            raise ContractValidationError("allowed_aliases must be list or tuple")

        return ModelContract(
            phase=payload["phase"],
            model=payload["model"],
            cli_profile=payload["cli_profile"],
            fallback_allowed=payload["fallback_allowed"],
            contract_version=payload["contract_version"],
            allowed_aliases=tuple(str(alias) for alias in aliases),
        )