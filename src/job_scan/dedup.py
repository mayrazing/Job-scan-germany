from __future__ import annotations

import hashlib
import re
from collections import defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from typing import Literal, TypeAlias
from urllib.parse import parse_qsl, unquote, urlsplit

from job_scan.domain import (
    AvailabilityEvent,
    AvailabilityStatus,
    DuplicateEvidence,
    JobRecord,
    MachineStatus,
    MergeEvidence,
    Snapshot,
    SourceKind,
    SourceOccurrence,
    UserStatus,
)
from job_scan.normalization import character_ngram_jaccard, normalize_job_url, normalize_text
from job_scan.sources.base import FetchedOccurrence

_SOURCE_ORDER = {
    SourceKind.LINKEDIN: 0,
    SourceKind.INDEED: 1,
    SourceKind.STEPSTONE: 2,
    SourceKind.GLASSDOOR: 3,
    SourceKind.SIMPLIFY: 4,
    SourceKind.ARBEITSAGENTUR: 5,
    SourceKind.BOSCH: 6,
    SourceKind.SMARTRECRUITERS: 7,
    SourceKind.TELEKOM: 8,
    SourceKind.SUCCESSFACTORS: 9,
    SourceKind.SIEMENS: 10,
    SourceKind.DHL: 11,
    SourceKind.THYSSENKRUPP: 12,
    SourceKind.DALLMEIER: 13,
    SourceKind.MANUAL: 14,
}
_AVAILABILITY_ORDER = {
    AvailabilityStatus.ACTIVE: 0,
    AvailabilityStatus.STALE: 1,
    AvailabilityStatus.CLOSED: 2,
}
_COLLECTION_PATH_ROLE_TOKENS = {
    "career",
    "careers",
    "job",
    "jobs",
    "opening",
    "openings",
    "opportunities",
    "opportunity",
    "position",
    "positions",
    "vacancies",
    "vacancy",
}
_SEARCH_PATH_ROLE_TOKENS = {
    "home",
    "index",
    "list",
    "listing",
    "listings",
    "result",
    "results",
    "search",
}
_APPLICATION_PATH_ROLE_TOKENS = {"apply", "application"}
_GENERIC_PATH_ROLE_TOKENS = (
    _COLLECTION_PATH_ROLE_TOKENS
    | _SEARCH_PATH_ROLE_TOKENS
    | _APPLICATION_PATH_ROLE_TOKENS
)
_GENERIC_PATH_MARKER_TOKEN_SEQUENCES = {
    ("general", "application"),
    ("login",),
    ("talent", "pool"),
    ("talentpool",),
    ("unsolicited", "application"),
}
_PAGE_SUFFIX_PATTERN = re.compile(r"[a-z]{1,6}")
_FILENAME_LOCALE_MODIFIER_PATTERN = re.compile(
    r"[a-z]{2,3}(?:[-_](?:[a-z]{2}|[a-z]{4}|[0-9]{3})){0,2}"
)
_FILENAME_VERSION_MODIFIER_PATTERN = re.compile(r"v[0-9]+(?:-[a-z0-9]+)*")
_FILENAME_VERSION_PART_PATTERN = re.compile(r"[0-9]+")
_PATH_WORD_TOKEN_PATTERN = re.compile(r"[^\W_]+")
_JOB_QUERY_KEYS = {"job", "jobid", "req", "requisitionid", "requisition_id"}
_MatchRule: TypeAlias = Literal["job_url", "text_similarity"]
_MATCH_RULE_PRIORITY: tuple[_MatchRule, ...] = ("job_url", "text_similarity")
_PERCENT_DECODE_ROUNDS = 4


@dataclass(frozen=True)
class _Match:
    rule: _MatchRule
    normalized_url: str | None
    normalized_company: str
    normalized_title: str
    normalized_location: str
    posted_at_delta_days: int | None
    similarity: float | None


@dataclass(frozen=True)
class _CandidateConflict:
    similarity: float | None
    decision_source_occurrence_key: str


_UrlOwners: TypeAlias = dict[str, dict[tuple[SourceKind, str], set[str]]]
_PairMatches: TypeAlias = dict[_MatchRule, _Match]
_CandidateMatches: TypeAlias = list[tuple[SourceOccurrence, _PairMatches]]
_SelectedMatches: TypeAlias = list[tuple[SourceOccurrence, _Match]]
_DuplicateInputSignature: TypeAlias = tuple[tuple[object, ...], ...]
_PathSegment: TypeAlias = tuple[str, ...]
_MarkerMatch: TypeAlias = Literal["none", "hard", "ambiguous"]


@dataclass(frozen=True)
class _MarkerSegmentVariant:
    tokens: _PathSegment
    page_tail: _MarkerMatch


def merge_occurrences(
    previous: Snapshot,
    current: Sequence[FetchedOccurrence],
    now: datetime,
) -> Snapshot:
    """Merge fetched occurrences without changing established canonical membership."""
    previous_duplicate_inputs = _duplicate_input_signatures(previous.jobs)
    snapshot = previous.model_copy(deep=True)
    selected = _select_current_occurrences(current)
    url_owners = _collect_url_owners(snapshot, selected)
    forced_conflicts: dict[tuple[str, str], _CandidateConflict] = {}
    unknown: list[tuple[str, FetchedOccurrence, SourceOccurrence, str | None]] = []

    for fetched in selected:
        stored = _stored_for_source_job_key(snapshot.jobs, fetched.source_job_key)
        latest = max((item for _, item in stored), key=lambda item: item.source_generation, default=None)
        rollover = latest is not None and requires_source_generation_rollover(
            latest, fetched
        )
        generation = (
            max((item.source_generation for _, item in stored), default=0) + 1
            if rollover
            else latest.source_generation if latest is not None else 1
        )

        if rollover and latest is not None:
            old_job = next(job for job, item in stored if item is latest)
            _close_for_rollover(latest, now)
            old_job.availability_status = _canonical_availability(old_job.source_occurrences)

        occurrence_key = f"{fetched.source_job_key}@{generation}"
        known = _find_occurrence(snapshot.jobs, occurrence_key)
        if known is not None and not rollover:
            job, occurrence = known
            reappeared = _update_known_occurrence(occurrence, fetched, now)
            job.last_seen = now
            job.last_error = fetched.fetch_error_code
            if reappeared:
                _invalidate_review(job, detail_complete=True)
            continue

        occurrence = _new_occurrence(fetched, generation, now)
        unknown.append(
            (occurrence.source_occurrence_key, fetched, occurrence, fetched.source_job_key if rollover else None)
        )

    for _, fetched, occurrence, excluded_key in sorted(unknown, key=lambda item: item[0]):
        if not occurrence.detail_complete:
            job = _new_job(occurrence, now)
            snapshot.jobs.append(job)
            continue

        raw = _raw_candidates(snapshot.jobs, occurrence, url_owners, excluded_key=excluded_key)
        admissible: dict[str, _SelectedMatches] = {}
        for key in raw:
            matches = _admissible(
                next(job for job in snapshot.jobs if job.canonical_job_key == key),
                occurrence,
                url_owners,
                excluded_key=excluded_key,
            )
            if matches is not None:
                admissible[key] = matches

        if len(admissible) == 1:
            canonical_key, matches = next(iter(admissible.items()))
            job = next(
                item for item in snapshot.jobs if item.canonical_job_key == canonical_key
            )
            occurrence.merge_evidence.extend(
                _merge_evidence(occurrence, other, match, now)
                for other, match in matches
            )
            job.source_occurrences.append(occurrence)
            job.last_seen = now
            job.last_error = fetched.fetch_error_code
            _invalidate_review(job, detail_complete=True)
        else:
            job = _new_job(occurrence, now)
            snapshot.jobs.append(job)
            if raw:
                for other_key, candidate_matches in raw.items():
                    forced_conflicts[
                        _ordered_pair(job.canonical_job_key, other_key)
                    ] = _CandidateConflict(
                        similarity=_candidate_similarity(candidate_matches),
                        decision_source_occurrence_key=occurrence.source_occurrence_key,
                    )

    for job in snapshot.jobs:
        _refresh_job(job)

    _recompute_possible_duplicates(
        snapshot.jobs,
        url_owners,
        now,
        forced_conflicts,
        previous_duplicate_inputs,
    )
    snapshot.jobs.sort(key=lambda job: job.canonical_job_key)
    return Snapshot(meta=snapshot.meta, jobs=snapshot.jobs)


def _select_current_occurrences(
    current: Sequence[FetchedOccurrence],
) -> list[FetchedOccurrence]:
    """Choose one deterministic, most complete value for each source job key."""
    by_key: dict[str, FetchedOccurrence] = {}
    for item in sorted(current, key=_fetched_sort_key):
        by_key[item.source_job_key] = item
    return [by_key[key] for key in sorted(by_key)]


def _fetched_sort_key(item: FetchedOccurrence) -> tuple[object, ...]:
    return (
        item.source_job_key,
        item.detail_complete,
        bool(item.description),
        len(normalize_text(item.description)),
        item.posted_at or date.min,
        str(item.url),
        item.company,
        item.title,
        item.location,
        item.fetch_error_code or "",
    )


def _stored_for_source_job_key(
    jobs: Iterable[JobRecord], source_job_key: str
) -> list[tuple[JobRecord, SourceOccurrence]]:
    return [
        (job, occurrence)
        for job in jobs
        for occurrence in job.source_occurrences
        if occurrence.source_job_key == source_job_key
    ]


def _find_occurrence(
    jobs: Iterable[JobRecord], source_occurrence_key: str
) -> tuple[JobRecord, SourceOccurrence] | None:
    for job in jobs:
        for occurrence in job.source_occurrences:
            if occurrence.source_occurrence_key == source_occurrence_key:
                return job, occurrence
    return None


def requires_source_generation_rollover(
    previous: SourceOccurrence,
    current: FetchedOccurrence,
) -> bool:
    """Return whether a reused source ID represents a genuinely new posting."""
    if not previous.detail_complete or not current.detail_complete:
        return False
    baseline_title = previous.identity_baseline_title or previous.title
    baseline_description = previous.identity_baseline_description or previous.description
    if normalize_text(baseline_title) == normalize_text(current.title):
        return False
    if character_ngram_jaccard(baseline_description, current.description) >= 0.30:
        return False
    if previous.availability_status is AvailabilityStatus.CLOSED:
        return True
    return bool(
        previous.posted_at is not None
        and current.posted_at is not None
        and (current.posted_at - previous.posted_at).days >= 60
    )


def _close_for_rollover(occurrence: SourceOccurrence, now: datetime) -> None:
    occurrence.availability_status = AvailabilityStatus.CLOSED
    if occurrence.closed_at is None:
        occurrence.closed_at = now
    occurrence.availability_events.append(
        AvailabilityEvent(
            status=AvailabilityStatus.CLOSED,
            reason="explicitly_closed",
            observed_at=now,
        )
    )


def _new_occurrence(
    fetched: FetchedOccurrence, generation: int, now: datetime
) -> SourceOccurrence:
    return SourceOccurrence(
        source=fetched.source,
        source_instance=fetched.source_instance,
        external_id=fetched.external_id,
        source_generation=generation,
        url=fetched.url,
        company=fetched.company,
        title=fetched.title,
        location=fetched.location,
        description=fetched.description,
        posted_at=fetched.posted_at,
        content_hash=fetched.content_hash,
        availability_status=AvailabilityStatus.ACTIVE,
        detail_complete=fetched.detail_complete,
        last_fetch_error_code=fetched.fetch_error_code,
        job_snapshot=fetched.job_snapshot,
        job_snapshot_error_code=fetched.job_snapshot_error_code,
        company_size_source=fetched.company_size_source,
        company_industry_source=fetched.company_industry_source,
        identity_baseline_title=fetched.title,
        identity_baseline_description=fetched.description,
        availability_events=[
            AvailabilityEvent(
                status=AvailabilityStatus.ACTIVE,
                reason="listed",
                observed_at=now,
            )
        ],
    )


def _update_known_occurrence(
    stored: SourceOccurrence, fetched: FetchedOccurrence, now: datetime
) -> bool:
    if not fetched.detail_complete and stored.detail_complete:
        stored.last_fetch_error_code = fetched.fetch_error_code
        return False

    was_complete = stored.detail_complete
    if was_complete:
        if not stored.identity_baseline_title:
            stored.identity_baseline_title = stored.title
        if not stored.identity_baseline_description:
            stored.identity_baseline_description = stored.description
    stored.url = fetched.url
    stored.company = fetched.company
    stored.title = fetched.title
    stored.location = fetched.location
    stored.description = fetched.description
    stored.content_hash = fetched.content_hash
    stored.detail_complete = fetched.detail_complete
    stored.last_fetch_error_code = fetched.fetch_error_code
    stored.company_size_source = fetched.company_size_source
    stored.company_industry_source = fetched.company_industry_source
    if stored.posted_at is None:
        stored.posted_at = fetched.posted_at
    if not was_complete:
        stored.identity_baseline_title = fetched.title
        stored.identity_baseline_description = fetched.description

    reappeared = (
        fetched.detail_complete
        and stored.availability_status is not AvailabilityStatus.ACTIVE
    )
    if reappeared:
        stored.availability_status = AvailabilityStatus.ACTIVE
        stored.closed_at = None
        stored.availability_events.append(
            AvailabilityEvent(
                status=AvailabilityStatus.ACTIVE,
                reason="reappeared",
                observed_at=now,
            )
        )
    return reappeared


def _new_job(occurrence: SourceOccurrence, now: datetime) -> JobRecord:
    key = _canonical_key(occurrence.source_occurrence_key)
    return JobRecord(
        canonical_job_key=key,
        source_occurrences=[occurrence],
        primary_source_occurrence_key=occurrence.source_occurrence_key,
        company=occurrence.company,
        title=occurrence.title,
        location=occurrence.location,
        url=occurrence.url,
        description=occurrence.description,
        posted_at=occurrence.posted_at,
        content_hash=occurrence.content_hash,
        first_seen=now,
        last_seen=now,
        availability_status=AvailabilityStatus.ACTIVE,
        machine_status=(
            MachineStatus.PENDING if occurrence.detail_complete else MachineStatus.PENDING_SOURCE
        ),
        user_status=UserStatus.NEW,
        user_status_updated_at=now,
        last_error=occurrence.last_fetch_error_code,
    )


def _canonical_key(minimum_source_occurrence_key: str) -> str:
    return hashlib.sha256(
        f"canonical\0{minimum_source_occurrence_key}".encode()
    ).hexdigest()


def _collect_url_owners(
    snapshot: Snapshot, current: Sequence[FetchedOccurrence]
) -> _UrlOwners:
    owners: _UrlOwners = defaultdict(lambda: defaultdict(set))
    for occurrence in (
        [item for job in snapshot.jobs for item in job.source_occurrences]
        + list(current)
    ):
        normalized = normalize_job_url(str(occurrence.url))
        owners[normalized][(occurrence.source, occurrence.source_instance)].add(
            occurrence.external_id
        )
    return {
        url: {namespace: set(ids) for namespace, ids in namespaces.items()}
        for url, namespaces in owners.items()
    }


def _raw_candidates(
    jobs: Iterable[JobRecord],
    occurrence: SourceOccurrence,
    url_owners: _UrlOwners,
    *,
    excluded_key: str | None,
    ignored_canonical_key: str | None = None,
) -> dict[str, _CandidateMatches]:
    candidates: dict[str, _CandidateMatches] = {}
    for job in jobs:
        if job.canonical_job_key == ignored_canonical_key:
            continue
        if excluded_key is not None and any(
            item.source_job_key == excluded_key for item in job.source_occurrences
        ):
            continue
        matches = [
            (item, pair_matches)
            for item in sorted(
                _active_complete(job), key=lambda value: value.source_occurrence_key
            )
            if (pair_matches := _high_confidence_matches(occurrence, item, url_owners))
        ]
        if matches:
            candidates[job.canonical_job_key] = matches
    return candidates


def _admissible(
    job: JobRecord,
    occurrence: SourceOccurrence,
    url_owners: _UrlOwners,
    *,
    excluded_key: str | None,
) -> _SelectedMatches | None:
    if excluded_key is not None and any(
        item.source_job_key == excluded_key for item in job.source_occurrences
    ):
        return None
    active = [
        item
        for item in job.source_occurrences
        if item.availability_status is AvailabilityStatus.ACTIVE
    ]
    if not active or not _cluster_member_invariant(active):
        return None
    namespace = (occurrence.source, occurrence.source_instance)
    if any(
        (item.source, item.source_instance) == namespace
        and item.source_job_key != occurrence.source_job_key
        for item in job.source_occurrences
    ):
        return None
    matches_by_member: list[tuple[SourceOccurrence, _PairMatches]] = []
    common_rules: set[_MatchRule] | None = None
    for item in sorted(active, key=lambda value: value.source_occurrence_key):
        if not item.detail_complete:
            return None
        pair_matches = _high_confidence_matches(occurrence, item, url_owners)
        if not pair_matches:
            return None
        matches_by_member.append((item, pair_matches))
        pair_rules = set(pair_matches)
        common_rules = (
            pair_rules if common_rules is None else common_rules & pair_rules
        )
        if not common_rules:
            return None
    assert common_rules is not None
    selected_rule = next(
        rule for rule in _MATCH_RULE_PRIORITY if rule in common_rules
    )
    return [
        (item, pair_matches[selected_rule])
        for item, pair_matches in matches_by_member
    ]


def _cluster_member_invariant(active: Sequence[SourceOccurrence]) -> bool:
    by_namespace: dict[tuple[SourceKind, str], str] = {}
    active_generations: set[str] = set()
    for item in active:
        namespace = (item.source, item.source_instance)
        known_key = by_namespace.setdefault(namespace, item.source_job_key)
        if known_key != item.source_job_key:
            return False
        if item.source_job_key in active_generations:
            return False
        active_generations.add(item.source_job_key)
    return True


def _active_complete(job: JobRecord) -> list[SourceOccurrence]:
    return [
        item
        for item in job.source_occurrences
        if item.availability_status is AvailabilityStatus.ACTIVE and item.detail_complete
    ]


def _high_confidence_matches(
    left: SourceOccurrence,
    right: SourceOccurrence,
    url_owners: _UrlOwners,
) -> _PairMatches:
    if not left.detail_complete or not right.detail_complete:
        return {}
    matches: _PairMatches = {}
    normalized_left_url = normalize_job_url(str(left.url))
    normalized_right_url = normalize_job_url(str(right.url))
    if (
        normalized_left_url == normalized_right_url
        and _job_specific_url(normalized_left_url, url_owners)
    ):
        matches["job_url"] = _match_value(
            left, right, "job_url", normalized_left_url
        )

    company = normalize_text(left.company)
    title = normalize_text(left.title)
    location = normalize_text(left.location)
    if (
        company != normalize_text(right.company)
        or title != normalize_text(right.title)
        or location != normalize_text(right.location)
        or left.posted_at is None
        or right.posted_at is None
    ):
        return matches
    delta = abs((left.posted_at - right.posted_at).days)
    if delta > 30:
        return matches
    similarity = character_ngram_jaccard(left.description, right.description)
    if similarity < 0.90:
        return matches
    matches["text_similarity"] = _Match(
        rule="text_similarity",
        normalized_url=None,
        normalized_company=company,
        normalized_title=title,
        normalized_location=location,
        posted_at_delta_days=delta,
        similarity=similarity,
    )
    return matches


def _match_value(
    left: SourceOccurrence,
    right: SourceOccurrence,
    rule: _MatchRule,
    normalized_url: str | None,
) -> _Match:
    delta = (
        abs((left.posted_at - right.posted_at).days)
        if left.posted_at is not None and right.posted_at is not None
        else None
    )
    similarity = character_ngram_jaccard(left.description, right.description)
    return _Match(
        rule=rule,
        normalized_url=normalized_url,
        normalized_company=normalize_text(left.company),
        normalized_title=normalize_text(left.title),
        normalized_location=normalize_text(left.location),
        posted_at_delta_days=delta,
        similarity=similarity,
    )


def _job_specific_url(url: str, url_owners: _UrlOwners) -> bool:
    parts = urlsplit(url)
    namespaces = url_owners.get(url, {})
    if any(len(external_ids) > 1 for external_ids in namespaces.values()):
        return False
    raw_segments = [segment for segment in _decode_path(parts.path).split("/") if segment]
    segments = [
        tokens
        for segment in raw_segments
        if (tokens := _path_segment_tokens(segment))
    ]
    marker_match = _generic_path_marker_match(raw_segments)
    if not segments or marker_match == "hard":
        return False
    query = {key.lower(): value for key, value in parse_qsl(parts.query)}
    if any(query.get(key) for key in _JOB_QUERY_KEYS):
        return True
    if marker_match == "ambiguous":
        return False
    terminal_tokens = _path_role_tokens(segments[-1])
    if terminal_tokens <= _APPLICATION_PATH_ROLE_TOKENS:
        collection_indexes = [
            index
            for index, segment in enumerate(segments[:-1])
            if _path_role_tokens(segment) <= _COLLECTION_PATH_ROLE_TOKENS
        ]
        if not collection_indexes:
            return False
        return any(
            not _path_role_tokens(segment) <= _GENERIC_PATH_ROLE_TOKENS
            for segment in segments[collection_indexes[-1] + 1 : -1]
        )
    if terminal_tokens <= _GENERIC_PATH_ROLE_TOKENS:
        return False
    return bool(namespaces)


def _path_role_tokens(segment: _PathSegment) -> set[str]:
    return set(segment)


def _generic_path_marker_match(raw_segments: Sequence[str]) -> _MarkerMatch:
    """Classify a terminal exact marker and its optional page tail."""
    if not raw_segments:
        return "none"
    final_variants = _marker_segment_token_variants(raw_segments[-1])
    ambiguous = False
    for marker in _GENERIC_PATH_MARKER_TOKEN_SEQUENCES:
        for start in range(len(raw_segments)):
            prefix_tokens = tuple(
                token
                for segment in raw_segments[start:-1]
                for token in _PATH_WORD_TOKEN_PATTERN.findall(
                    _path_segment_value(segment)
                )
            )
            for variant in final_variants:
                if prefix_tokens + variant.tokens != marker:
                    continue
                if variant.page_tail == "ambiguous":
                    ambiguous = True
                else:
                    return "hard"
    return "ambiguous" if ambiguous else "none"


def _marker_segment_token_variants(
    segment: str,
) -> tuple[_MarkerSegmentVariant, ...]:
    """Return full tokens and classified variants before a valid page tail."""
    value = _path_segment_value(segment)
    components = value.split(".")
    variants = [
        _MarkerSegmentVariant(
            tokens=tuple(_PATH_WORD_TOKEN_PATTERN.findall(value)),
            page_tail="none",
        )
    ]
    for boundary in range(1, len(components)):
        page_tail = components[boundary:]
        if not _has_page_suffix(("marker", *page_tail)):
            continue
        semantic_value = ".".join(components[:boundary])
        has_version = any(
            _FILENAME_VERSION_MODIFIER_PATTERN.fullmatch(component)
            for component in page_tail[:-1]
        )
        variants.append(
            _MarkerSegmentVariant(
                tokens=tuple(_PATH_WORD_TOKEN_PATTERN.findall(semantic_value)),
                page_tail="ambiguous" if has_version else "hard",
            )
        )
    return tuple(variants)


def _path_segment_tokens(segment: str) -> _PathSegment:
    """Return semantic tokens after removing matrix params and a valid file suffix."""
    value = _path_segment_value(segment)
    components = value.split(".")
    if _has_page_suffix(components):
        value = components[0]
    return tuple(_PATH_WORD_TOKEN_PATTERN.findall(value))


def _path_segment_value(segment: str) -> str:
    """Return a lowercase logical segment without matrix parameters."""
    return segment.split(";", 1)[0].lower()


def _has_page_suffix(components: Sequence[str]) -> bool:
    """Return whether dot components follow basename, modifiers, and page suffix grammar."""
    return bool(
        len(components) >= 2
        and components[0]
        and _PAGE_SUFFIX_PATTERN.fullmatch(components[-1])
        and _has_filename_modifiers(components[1:-1])
    )


def _has_filename_modifiers(components: Sequence[str]) -> bool:
    """Return whether components are locale, minification, or version modifiers."""
    index = 0
    while index < len(components):
        component = components[index]
        if component == "min" or _FILENAME_LOCALE_MODIFIER_PATTERN.fullmatch(component):
            index += 1
            continue
        if _FILENAME_VERSION_MODIFIER_PATTERN.fullmatch(component):
            index += 1
            while index < len(components) and _FILENAME_VERSION_PART_PATTERN.fullmatch(
                components[index]
            ):
                index += 1
            continue
        return False
    return True


def _decode_path(path: str) -> str:
    decoded = path
    for _ in range(_PERCENT_DECODE_ROUNDS):
        next_value = unquote(decoded)
        if next_value == decoded:
            break
        decoded = next_value
    return decoded


def _merge_evidence(
    occurrence: SourceOccurrence,
    other: SourceOccurrence,
    match: _Match,
    now: datetime,
) -> MergeEvidence:
    return MergeEvidence(
        other_source_occurrence_key=other.source_occurrence_key,
        rule=match.rule,
        normalized_url=match.normalized_url,
        normalized_company=match.normalized_company,
        normalized_title=match.normalized_title,
        normalized_location=match.normalized_location,
        posted_at_delta_days=match.posted_at_delta_days,
        similarity=match.similarity,
        observed_at=now,
    )


def _refresh_job(job: JobRecord) -> None:
    old_hash = job.content_hash
    primary = min(job.source_occurrences, key=_primary_key)
    job.primary_source_occurrence_key = primary.source_occurrence_key
    job.company = primary.company
    job.title = primary.title
    job.location = primary.location
    job.url = primary.url
    job.description = primary.description
    job.posted_at = primary.posted_at
    job.content_hash = primary.content_hash
    job.availability_status = _canonical_availability(job.source_occurrences)
    if job.content_hash != old_hash:
        _invalidate_review(job, primary.detail_complete)


def _primary_key(occurrence: SourceOccurrence) -> tuple[object, ...]:
    core_count = sum(
        (
            bool(str(occurrence.url)),
            bool(occurrence.company.strip()),
            bool(occurrence.title.strip()),
            bool(occurrence.location.strip()),
            bool(occurrence.detail_complete and occurrence.description.strip()),
            occurrence.posted_at is not None,
        )
    )
    return (
        _AVAILABILITY_ORDER[occurrence.availability_status],
        not occurrence.detail_complete,
        occurrence.source is SourceKind.ARBEITSAGENTUR,
        -core_count,
        -len(normalize_text(occurrence.description)),
        -(occurrence.posted_at.toordinal() if occurrence.posted_at else 0),
        _SOURCE_ORDER[occurrence.source],
        occurrence.source_occurrence_key,
    )


def _canonical_availability(
    occurrences: Sequence[SourceOccurrence],
) -> AvailabilityStatus:
    statuses = {item.availability_status for item in occurrences}
    if AvailabilityStatus.ACTIVE in statuses:
        return AvailabilityStatus.ACTIVE
    if AvailabilityStatus.STALE in statuses:
        return AvailabilityStatus.STALE
    return AvailabilityStatus.CLOSED


def _invalidate_review(job: JobRecord, detail_complete: bool) -> None:
    job.machine_status = (
        MachineStatus.PENDING if detail_complete else MachineStatus.PENDING_SOURCE
    )
    job.manual_override = None
    job.manual_override_content_hash = None
    job.manual_override_profile_hash = None
    job.ai_review = None
    job.score = None
    job.reason = ""
    job.review_model = None
    job.reviewed_at = None
    job.exclusion_reasons = []
    job.labels = [label for label in job.labels if label == "Possible duplicate"]


def _recompute_possible_duplicates(
    jobs: Sequence[JobRecord],
    url_owners: _UrlOwners,
    now: datetime,
    forced_conflicts: dict[tuple[str, str], _CandidateConflict],
    previous_inputs: dict[str, _DuplicateInputSignature],
) -> None:
    by_key = {job.canonical_job_key: job for job in jobs}
    current_inputs = _duplicate_input_signatures(jobs)
    existing_directed = {
        (
            job.canonical_job_key,
            evidence.other_canonical_job_key,
            evidence.reason,
            evidence.similarity,
            evidence.decision_source_occurrence_key,
        ): evidence.observed_at
        for job in jobs
        for evidence in job.possible_duplicates
    }
    existing_candidate_decisions: dict[tuple[str, str], set[str | None]] = defaultdict(set)
    for job in jobs:
        for evidence in job.possible_duplicates:
            if (
                evidence.reason != "candidate_conflict"
                or evidence.other_canonical_job_key not in by_key
                or evidence.other_canonical_job_key == job.canonical_job_key
            ):
                continue
            existing_candidate_decisions[
                _ordered_pair(job.canonical_job_key, evidence.other_canonical_job_key)
            ].add(evidence.decision_source_occurrence_key)
    edges: dict[
        tuple[str, str],
        tuple[
            Literal["candidate_conflict", "similarity_band"],
            float | None,
            str | None,
        ],
    ] = {}

    for index, left in enumerate(jobs):
        for right in jobs[index + 1 :]:
            similarity = _similarity_band_between(left, right)
            if similarity is not None:
                edges[_ordered_pair(left.canonical_job_key, right.canonical_job_key)] = (
                    "similarity_band",
                    similarity,
                    None,
                )

    for pair in sorted(existing_candidate_decisions.keys() - forced_conflicts.keys()):
        persisted_keys = {
            key for key in existing_candidate_decisions[pair] if key is not None
        }
        if len(persisted_keys) > 1:
            continue
        decision_key = next(iter(persisted_keys), None)
        validated_similarity = _revalidate_candidate_conflict(
            pair,
            jobs,
            by_key,
            url_owners,
            decision_key,
        )
        if validated_similarity is not None:
            edges[pair] = (
                "candidate_conflict",
                validated_similarity,
                decision_key,
            )

    for pair, forced_conflict in forced_conflicts.items():
        edges[pair] = (
            "candidate_conflict",
            forced_conflict.similarity,
            forced_conflict.decision_source_occurrence_key,
        )

    directed: dict[str, list[DuplicateEvidence]] = defaultdict(list)
    for (left_key, right_key), (reason, similarity, decision_key) in sorted(edges.items()):
        left_observed_at = existing_directed.get(
            (left_key, right_key, reason, similarity, decision_key)
        )
        right_observed_at = existing_directed.get(
            (right_key, left_key, reason, similarity, decision_key)
        )
        observed_at = (
            left_observed_at
            if (
                left_observed_at is not None
                and left_observed_at == right_observed_at
                and previous_inputs.get(left_key) == current_inputs[left_key]
                and previous_inputs.get(right_key) == current_inputs[right_key]
            )
            else now
        )
        directed[left_key].append(
            DuplicateEvidence(
                other_canonical_job_key=right_key,
                reason=reason,
                similarity=similarity,
                observed_at=observed_at,
                decision_source_occurrence_key=decision_key,
            )
        )
        directed[right_key].append(
            DuplicateEvidence(
                other_canonical_job_key=left_key,
                reason=reason,
                similarity=similarity,
                observed_at=observed_at,
                decision_source_occurrence_key=decision_key,
            )
        )

    for job in jobs:
        job.possible_duplicates = sorted(
            directed.get(job.canonical_job_key, []),
            key=lambda evidence: (evidence.other_canonical_job_key, evidence.reason),
        )
        job.labels = [label for label in job.labels if label != "Possible duplicate"]
        if job.possible_duplicates:
            job.labels.append("Possible duplicate")


def _revalidate_candidate_conflict(
    pair: tuple[str, str],
    jobs: Sequence[JobRecord],
    by_key: dict[str, JobRecord],
    url_owners: _UrlOwners,
    decision_source_occurrence_key: str | None,
) -> float | None:
    """Revalidate one stored decision edge without deriving any new pair."""
    left, right = (by_key[pair[0]], by_key[pair[1]])
    if decision_source_occurrence_key is None:
        home, other = max(
            ((left, right), (right, left)),
            key=lambda value: _canonical_decision_order(value[0]),
        )
        occurrence = _canonical_decision_occurrence(home)
    else:
        located = _find_occurrence(jobs, decision_source_occurrence_key)
        if located is None:
            return None
        home, occurrence = located
        if home.canonical_job_key == left.canonical_job_key:
            other = right
        elif home.canonical_job_key == right.canonical_job_key:
            other = left
        else:
            return None
    if (
        occurrence is None
        or occurrence.availability_status is not AvailabilityStatus.ACTIVE
        or not occurrence.detail_complete
    ):
        return None
    excluded_key = (
        occurrence.source_job_key
        if decision_source_occurrence_key is not None
        else None
    )
    raw = _raw_candidates(
        jobs,
        occurrence,
        url_owners,
        excluded_key=excluded_key,
        ignored_canonical_key=home.canonical_job_key,
    )
    matches = raw.get(other.canonical_job_key)
    if matches is None:
        return None
    admissible_count = sum(
        _admissible(by_key[key], occurrence, url_owners, excluded_key=excluded_key)
        is not None
        for key in raw
    )
    if admissible_count == 1:
        return None
    return _candidate_similarity(matches)


def _canonical_decision_order(job: JobRecord) -> tuple[datetime, str]:
    occurrence = _canonical_decision_occurrence(job)
    return (
        job.first_seen,
        occurrence.source_occurrence_key if occurrence is not None else "",
    )


def _canonical_decision_occurrence(job: JobRecord) -> SourceOccurrence | None:
    derived = [
        occurrence
        for occurrence in job.source_occurrences
        if _canonical_key(occurrence.source_occurrence_key)
        == job.canonical_job_key
    ]
    if derived:
        return min(derived, key=lambda item: item.source_occurrence_key)
    created = [
        occurrence
        for occurrence in job.source_occurrences
        if any(
            event.reason == "listed" and event.observed_at == job.first_seen
            for event in occurrence.availability_events
        )
    ]
    candidates = created or job.source_occurrences
    return min(candidates, key=lambda item: item.source_occurrence_key, default=None)


def _candidate_similarity(matches: _CandidateMatches) -> float | None:
    return max(
        (
            match.similarity
            for _, pair_matches in matches
            for match in pair_matches.values()
            if match.similarity is not None
        ),
        default=None,
    )


def _duplicate_input_signatures(
    jobs: Sequence[JobRecord],
) -> dict[str, _DuplicateInputSignature]:
    return {
        job.canonical_job_key: tuple(
            sorted(
                (
                    occurrence.source_occurrence_key,
                    occurrence.availability_status.value,
                    occurrence.detail_complete,
                    normalize_job_url(str(occurrence.url)),
                    normalize_text(occurrence.company),
                    normalize_text(occurrence.title),
                    normalize_text(occurrence.location),
                    occurrence.posted_at.isoformat() if occurrence.posted_at else None,
                    normalize_text(occurrence.description),
                    occurrence.content_hash,
                )
                for occurrence in job.source_occurrences
            )
        )
        for job in jobs
    }


def _similarity_band_between(left: JobRecord, right: JobRecord) -> float | None:
    similarities = [
        similarity
        for left_occurrence in _active_complete(left)
        for right_occurrence in _active_complete(right)
        if (similarity := _similarity_band(left_occurrence, right_occurrence)) is not None
    ]
    return max(similarities, default=None)


def _similarity_band(
    left: SourceOccurrence, right: SourceOccurrence
) -> float | None:
    if (
        normalize_text(left.company) != normalize_text(right.company)
        or normalize_text(left.title) != normalize_text(right.title)
        or normalize_text(left.location) != normalize_text(right.location)
        or left.posted_at is None
        or right.posted_at is None
        or abs((left.posted_at - right.posted_at).days) > 30
    ):
        return None
    similarity = character_ngram_jaccard(left.description, right.description)
    return similarity if 0.70 <= similarity < 0.90 else None


def _ordered_pair(left: str, right: str) -> tuple[str, str]:
    return (left, right) if left < right else (right, left)
