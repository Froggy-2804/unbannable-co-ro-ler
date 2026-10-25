"""Data models, enums, JSON schema, and invariant validation for confession sentiment analysis."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

try:
    import jsonschema
except ImportError:
    jsonschema = None

SENTIMENT_VALUES: frozenset[str] = frozenset({"positive", "negative", "neutral"})
SCOPE_VALUES: frozenset[str] = frozenset(
    {"confession", "post_general", "commenter", "off_topic", "unresolved"}
)
LINK_METHOD_VALUES: frozenset[str] = frozenset(
    {"explicit_reference", "conversation_context", "semantic_inference"}
)
SOURCE_TYPE_VALUES: frozenset[str] = frozenset(
    {"confession", "ancestor_comment", "target_comment"}
)
REASON_CODE_VALUES: frozenset[str] = frozenset(
    {
        "ambiguous_target",
        "ambiguous_sentiment",
        "mixed_sentiment",
        "missing_context",
        "incomplete_source",
    }
)


@dataclass(slots=True, frozen=True)
class ConfessionItem:
    confession_key: str
    confession_number: str
    text: str


@dataclass(slots=True, frozen=True)
class AncestorCommentItem:
    comment_key: str
    parent_comment_key: str | None
    text: str


@dataclass(slots=True, frozen=True)
class TargetCommentItem:
    comment_key: str
    parent_comment_key: str | None
    text: str


@dataclass(slots=True, frozen=True)
class AnalysisInput:
    post_key: str
    confessions: list[ConfessionItem]
    ancestor_comments: list[AncestorCommentItem]
    target_comment: TargetCommentItem
    context_warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True, frozen=True)
class BatchCommentTarget:
    comment_key: str
    parent_comment_key: str | None
    text: str
    ancestor_comments: list[AncestorCommentItem] = field(default_factory=list)
    context_warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "comment_key": self.comment_key,
            "parent_comment_key": self.parent_comment_key,
            "text": self.text,
            "ancestor_comments": [asdict(a) for a in self.ancestor_comments],
            "context_warnings": self.context_warnings,
        }


@dataclass(slots=True, frozen=True)
class BatchAnalysisInput:
    post_key: str
    confessions: list[ConfessionItem]
    comments: list[BatchCommentTarget]

    def to_dict(self) -> dict[str, Any]:
        return {
            "post_key": self.post_key,
            "confessions": [asdict(c) for c in self.confessions],
            "comments": [c.to_dict() for c in self.comments],
        }


@dataclass(slots=True, frozen=True)
class EvidenceItem:
    source_type: str  # confession | ancestor_comment | target_comment
    source_key: str
    quote: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True, frozen=True)
class AnalysisTargetItem:
    confession_key: str
    sentiment: str | None  # positive | negative | neutral | None
    link_method: str  # explicit_reference | conversation_context | semantic_inference
    link_evidence: list[EvidenceItem]
    sentiment_evidence: list[EvidenceItem]

    def to_dict(self) -> dict[str, Any]:
        return {
            "confession_key": self.confession_key,
            "sentiment": self.sentiment,
            "link_method": self.link_method,
            "link_evidence": [e.to_dict() for e in self.link_evidence],
            "sentiment_evidence": [e.to_dict() for e in self.sentiment_evidence],
        }


@dataclass(slots=True, frozen=True)
class AnalysisOutput:
    comment_key: str
    scope: str  # confession | post_general | commenter | off_topic | unresolved
    targets: list[AnalysisTargetItem]
    review_required: bool
    reason_code: str | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "comment_key": self.comment_key,
            "scope": self.scope,
            "targets": [t.to_dict() for t in self.targets],
            "review_required": self.review_required,
            "reason_code": self.reason_code,
        }


ANALYSIS_OUTPUT_JSON_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "comment_key": {"type": "string"},
        "scope": {
            "type": "string",
            "enum": ["confession", "post_general", "commenter", "off_topic", "unresolved"],
        },
        "targets": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "confession_key": {"type": "string"},
                    "sentiment": {
                        "type": ["string", "null"],
                        "enum": ["positive", "negative", "neutral", None],
                    },
                    "link_method": {
                        "type": "string",
                        "enum": ["explicit_reference", "conversation_context", "semantic_inference"],
                    },
                    "link_evidence": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "source_type": {
                                    "type": "string",
                                    "enum": ["confession", "ancestor_comment", "target_comment"],
                                },
                                "source_key": {"type": "string"},
                                "quote": {"type": "string", "minLength": 1},
                            },
                            "required": ["source_type", "source_key", "quote"],
                            "additionalProperties": False,
                        },
                    },
                    "sentiment_evidence": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "source_type": {
                                    "type": "string",
                                    "enum": ["confession", "ancestor_comment", "target_comment"],
                                },
                                "source_key": {"type": "string"},
                                "quote": {"type": "string", "minLength": 1},
                            },
                            "required": ["source_type", "source_key", "quote"],
                            "additionalProperties": False,
                        },
                    },
                },
                "required": [
                    "confession_key",
                    "sentiment",
                    "link_method",
                    "link_evidence",
                    "sentiment_evidence",
                ],
                "additionalProperties": False,
            },
        },
        "review_required": {"type": "boolean"},
        "reason_code": {
            "type": ["string", "null"],
            "enum": [
                "ambiguous_target",
                "ambiguous_sentiment",
                "mixed_sentiment",
                "missing_context",
                "incomplete_source",
                None,
            ],
        },
    },
    "required": ["comment_key", "scope", "targets", "review_required", "reason_code"],
    "additionalProperties": False,
}

BATCH_ANALYSIS_OUTPUT_JSON_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "results": {
            "type": "array",
            "items": ANALYSIS_OUTPUT_JSON_SCHEMA,
        }
    },
    "required": ["results"],
    "additionalProperties": False,
}

_SINGLE_VALIDATOR = (
    jsonschema.Draft7Validator(ANALYSIS_OUTPUT_JSON_SCHEMA)
    if jsonschema is not None
    else None
)
_BATCH_VALIDATOR = (
    jsonschema.Draft7Validator(BATCH_ANALYSIS_OUTPUT_JSON_SCHEMA)
    if jsonschema is not None
    else None
)


def parse_analysis_output(data: dict[str, Any]) -> AnalysisOutput:
    """Construct an AnalysisOutput instance from raw JSON-like dictionary."""
    targets = []
    for t in data.get("targets") or []:
        if not isinstance(t, dict):
            continue
        link_ev = [
            EvidenceItem(
                source_type=e.get("source_type", ""),
                source_key=e.get("source_key", ""),
                quote=e.get("quote", ""),
            )
            for e in t.get("link_evidence") or []
            if isinstance(e, dict)
        ]
        sent_ev = [
            EvidenceItem(
                source_type=e.get("source_type", ""),
                source_key=e.get("source_key", ""),
                quote=e.get("quote", ""),
            )
            for e in t.get("sentiment_evidence") or []
            if isinstance(e, dict)
        ]
        targets.append(
            AnalysisTargetItem(
                confession_key=t.get("confession_key", ""),
                sentiment=t.get("sentiment"),
                link_method=t.get("link_method", ""),
                link_evidence=link_ev,
                sentiment_evidence=sent_ev,
            )
        )
    return AnalysisOutput(
        comment_key=data.get("comment_key", ""),
        scope=data.get("scope", ""),
        targets=targets,
        review_required=bool(data.get("review_required", False)),
        reason_code=data.get("reason_code"),
    )


def find_best_quote(quote: str, source_text: str) -> str | None:
    """Return exact quote if found in source text, otherwise None."""
    if not quote or not source_text:
        return None
    if quote in source_text:
        return quote
    return None


def validate_analysis_output(
    output_data: dict[str, Any] | AnalysisOutput,
    input_data: dict[str, Any] | AnalysisInput,
) -> tuple[bool, str | None]:
    """Validate all structural, attribution, and textual invariants of model output.
    Returns:
        (True, None) if completely valid.
        (False, error_description) if any invariant fails.
    """
    if isinstance(output_data, AnalysisOutput):
        out_dict = output_data.to_dict()
    elif isinstance(output_data, dict):
        out_dict = output_data
    else:
        return False, f"output_data must be a dict or AnalysisOutput, got {type(output_data).__name__}"

    if isinstance(input_data, AnalysisInput):
        inp_dict = input_data.to_dict()
    elif isinstance(input_data, dict):
        inp_dict = input_data
    else:
        return False, f"input_data must be a dict or AnalysisInput, got {type(input_data).__name__}"

    # Schema validation first (jsonschema Draft7Validator if available)
    if _SINGLE_VALIDATOR is not None:
        errors = list(_SINGLE_VALIDATOR.iter_errors(out_dict))
        if errors:
            first_err = errors[0]
            path_str = ".".join(str(p) for p in first_err.path)
            prefix = f"{path_str}: " if path_str else ""
            return False, f"Schema error: {prefix}{first_err.message}"

    # Structural checks (total on arbitrary decoded JSON)
    for req_field in ("comment_key", "scope", "targets", "review_required", "reason_code"):
        if req_field not in out_dict:
            return False, f"Missing required field '{req_field}'"
    for k in out_dict:
        if k not in ("comment_key", "scope", "targets", "review_required", "reason_code"):
            return False, f"Unexpected extra field '{k}'"

    comment_key = out_dict.get("comment_key")
    if not isinstance(comment_key, str):
        return False, f"comment_key must be a string, got {type(comment_key).__name__}"

    scope = out_dict.get("scope")
    if scope not in SCOPE_VALUES:
        return False, f"Invalid scope '{scope}'"

    review_required = out_dict.get("review_required")
    if not isinstance(review_required, bool):
        return False, f"review_required must be a boolean, got {type(review_required).__name__}"

    reason_code = out_dict.get("reason_code")
    if reason_code is not None and reason_code not in REASON_CODE_VALUES:
        return False, f"Invalid reason_code '{reason_code}'"

    targets = out_dict.get("targets")
    if not isinstance(targets, list):
        return False, f"targets must be a list, got {type(targets).__name__}"

    target_comment = inp_dict.get("target_comment")
    if not isinstance(target_comment, dict):
        return False, "input_data missing valid 'target_comment' dict"
    expected_comment_key = target_comment.get("comment_key")
    if comment_key != expected_comment_key:
        return False, f"comment_key '{comment_key}' does not match target '{expected_comment_key}'"

    if scope == "unresolved":
        if not review_required:
            return False, "scope 'unresolved' requires review_required=True"
        if not reason_code:
            return False, "scope 'unresolved' requires a reason_code"

    if scope == "confession" and len(targets) == 0:
        return False, "scope 'confession' must contain at least one target"
    if scope != "confession" and len(targets) > 0:
        return False, f"scope '{scope}' must have empty targets list"

    # Index available sources for evidence verification
    confessions_list = inp_dict.get("confessions") or []
    ancestor_list = inp_dict.get("ancestor_comments") or []

    confession_sources: dict[str, str] = {
        c["confession_key"]: c["text"]
        for c in confessions_list
        if isinstance(c, dict) and "confession_key" in c and isinstance(c.get("text"), str)
    }
    ancestor_sources: dict[str, str] = {
        a["comment_key"]: a["text"]
        for a in ancestor_list
        if isinstance(a, dict) and "comment_key" in a and isinstance(a.get("text"), str)
    }
    target_sources: dict[str, str] = {
        expected_comment_key: target_comment.get("text", "")
    }

    sources_map: dict[str, dict[str, str]] = {
        "confession": confession_sources,
        "ancestor_comment": ancestor_sources,
        "target_comment": target_sources,
    }

    seen_confession_keys: set[str] = set()
    for idx, target in enumerate(targets):
        if not isinstance(target, dict):
            return False, f"target[{idx}] must be a dict"
        for k in target:
            if k not in ("confession_key", "sentiment", "link_method", "link_evidence", "sentiment_evidence"):
                return False, f"target[{idx}] unexpected extra field '{k}'"

        cfs_key = target.get("confession_key")
        if not isinstance(cfs_key, str) or cfs_key not in confession_sources:
            return False, f"target[{idx}] confession_key '{cfs_key}' not found in post confessions"

        if cfs_key in seen_confession_keys:
            return False, f"Duplicate confession_key '{cfs_key}' in targets"
        seen_confession_keys.add(cfs_key)

        sentiment = target.get("sentiment")
        if sentiment is not None and sentiment not in SENTIMENT_VALUES:
            return False, f"target[{idx}] invalid sentiment '{sentiment}'"

        if sentiment is None:
            if not review_required:
                return False, f"target[{idx}] has null sentiment but review_required is False"
            if not reason_code:
                return False, f"target[{idx}] has null sentiment but reason_code is missing"

        link_method = target.get("link_method")
        if link_method not in LINK_METHOD_VALUES:
            return False, f"target[{idx}] invalid link_method '{link_method}'"

        # Validate link_evidence
        link_evidence = target.get("link_evidence")
        if not isinstance(link_evidence, list) or len(link_evidence) == 0:
            return False, f"target[{idx}] must have non-empty link_evidence"

        for ev_idx, ev in enumerate(link_evidence):
            if not isinstance(ev, dict):
                return False, f"target[{idx}] link_evidence[{ev_idx}] must be a dict"
            for k in ev:
                if k not in ("source_type", "source_key", "quote"):
                    return False, f"target[{idx}] link_evidence[{ev_idx}] unexpected extra field '{k}'"
            stype = ev.get("source_type")
            skey = ev.get("source_key")
            quote = ev.get("quote")
            if stype not in SOURCE_TYPE_VALUES:
                return False, f"target[{idx}] link_evidence[{ev_idx}] invalid source_type '{stype}'"
            if skey not in sources_map.get(stype, {}):
                return False, f"target[{idx}] link_evidence[{ev_idx}] source_key '{skey}' not found in {stype}"
            if not isinstance(quote, str) or not quote:
                return False, f"target[{idx}] link_evidence[{ev_idx}] quote must be a non-empty string"
            source_content = sources_map[stype][skey]
            if quote not in source_content:
                return False, f"target[{idx}] link_evidence[{ev_idx}] quote '{quote}' not found in source text"

        # Validate sentiment_evidence
        sent_evidence = target.get("sentiment_evidence")
        if not isinstance(sent_evidence, list):
            return False, f"target[{idx}] sentiment_evidence must be a list"

        if sentiment is not None:
            if len(sent_evidence) == 0:
                return False, f"target[{idx}] has sentiment '{sentiment}' but empty sentiment_evidence"
            has_target_quote = False
            for ev_idx, ev in enumerate(sent_evidence):
                if not isinstance(ev, dict):
                    return False, f"target[{idx}] sentiment_evidence[{ev_idx}] must be a dict"
                for k in ev:
                    if k not in ("source_type", "source_key", "quote"):
                        return False, f"target[{idx}] sentiment_evidence[{ev_idx}] unexpected extra field '{k}'"
                stype = ev.get("source_type")
                skey = ev.get("source_key")
                quote = ev.get("quote")
                if stype not in SOURCE_TYPE_VALUES:
                    return False, f"target[{idx}] sentiment_evidence[{ev_idx}] invalid source_type '{stype}'"
                if skey not in sources_map.get(stype, {}):
                    return False, f"target[{idx}] sentiment_evidence[{ev_idx}] source_key '{skey}' not found in {stype}"
                if not isinstance(quote, str) or not quote:
                    return False, f"target[{idx}] sentiment_evidence[{ev_idx}] quote must be a non-empty string"
                source_content = sources_map[stype][skey]
                if quote not in source_content:
                    return False, f"target[{idx}] sentiment_evidence[{ev_idx}] quote '{quote}' not found in source text"
                if stype == "target_comment" and skey == expected_comment_key:
                    has_target_quote = True
            if not has_target_quote:
                return False, f"target[{idx}] sentiment_evidence must include at least one quote from target_comment"
        else:
            # sentiment is None: still validate ANY supplied sentiment_evidence!
            for ev_idx, ev in enumerate(sent_evidence):
                if not isinstance(ev, dict):
                    return False, f"target[{idx}] sentiment_evidence[{ev_idx}] must be a dict"
                for k in ev:
                    if k not in ("source_type", "source_key", "quote"):
                        return False, f"target[{idx}] sentiment_evidence[{ev_idx}] unexpected extra field '{k}'"
                stype = ev.get("source_type")
                skey = ev.get("source_key")
                quote = ev.get("quote")
                if stype not in SOURCE_TYPE_VALUES:
                    return False, f"target[{idx}] sentiment_evidence[{ev_idx}] invalid source_type '{stype}'"
                if skey not in sources_map.get(stype, {}):
                    return False, f"target[{idx}] sentiment_evidence[{ev_idx}] source_key '{skey}' not found in {stype}"
                if not isinstance(quote, str) or not quote:
                    return False, f"target[{idx}] sentiment_evidence[{ev_idx}] quote must be a non-empty string"
                source_content = sources_map[stype][skey]
                if quote not in source_content:
                    return False, f"target[{idx}] sentiment_evidence[{ev_idx}] quote '{quote}' not found in source text"

    return True, None


def validate_batch_analysis_output(
    batch_output: Any,
    batch_input: dict[str, Any] | BatchAnalysisInput,
) -> tuple[dict[str, dict[str, Any]], list[tuple[str, str]]]:
    """Validate a batch of model outputs against each corresponding comment input.

    Returns:
        (valid_by_key, errors_by_key)
        - valid_by_key: {comment_key: valid_output_dict}
        - errors_by_key: [(comment_key, error_message)]
        Errors are strictly keyed ONLY by actual requested comment keys.
    """
    if isinstance(batch_input, BatchAnalysisInput):
        inp_dict = batch_input.to_dict()
    elif isinstance(batch_input, dict):
        inp_dict = batch_input
    else:
        inp_dict = {}

    post_confessions = inp_dict.get("confessions", [])
    comments_list = inp_dict.get("comments", [])
    comments_by_key: dict[str, dict[str, Any]] = {
        c["comment_key"]: c
        for c in comments_list
        if isinstance(c, dict) and "comment_key" in c and isinstance(c["comment_key"], str)
    }
    requested_keys = list(comments_by_key.keys())

    # Check malformed envelope
    if not isinstance(batch_output, dict):
        return {}, [(ck, "Malformed batch envelope: output must be a JSON object") for ck in requested_keys]

    results = batch_output.get("results")
    if not isinstance(results, list):
        return {}, [(ck, "Malformed batch envelope: missing 'results' list") for ck in requested_keys]

    valid_by_key: dict[str, dict[str, Any]] = {}
    errors_by_key: list[tuple[str, str]] = []

    # Detect duplicates and group results by comment_key for requested keys
    items_by_key: dict[str, list[dict[str, Any]]] = {}
    for item in results:
        if not isinstance(item, dict):
            continue
        ck = item.get("comment_key")
        if not isinstance(ck, str) or ck not in comments_by_key:
            # Unknown IDs or invalid comment_key: ignored, never attached
            continue
        items_by_key.setdefault(ck, []).append(item)

    # Process all requested keys deterministically
    for ck in requested_keys:
        items = items_by_key.get(ck)
        if not items:
            errors_by_key.append((ck, "Comment omitted from model response"))
            continue
        if len(items) > 1:
            errors_by_key.append((ck, f"Duplicate comment_key '{ck}' in batch results (ambiguity error)"))
            continue

        item = items[0]
        c_target = comments_by_key[ck]
        single_input = {
            "post_key": inp_dict.get("post_key", ""),
            "confessions": post_confessions,
            "ancestor_comments": c_target.get("ancestor_comments", []),
            "target_comment": {
                "comment_key": ck,
                "parent_comment_key": c_target.get("parent_comment_key"),
                "text": c_target.get("text", ""),
            },
            "context_warnings": c_target.get("context_warnings", []),
        }

        is_valid, err = validate_analysis_output(item, single_input)
        if is_valid:
            valid_by_key[ck] = item
        else:
            errors_by_key.append((ck, err or "Validation failed"))

    return valid_by_key, errors_by_key
