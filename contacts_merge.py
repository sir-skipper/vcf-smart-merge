#!/usr/bin/env python3
"""
Conservative VCF merge/enrichment tool.

Usage:
  python contacts_merge_v2.py baseline.vcf source1.vcf source2.vcf ...

Outputs:
  merged_contacts.vcf
  missing_contacts.json
  merge_report.txt

Design principles:
  - First VCF argument is the baseline/authority.
  - Existing baseline contacts/fields are never reformatted or rewritten.
  - Later sources enrich the current merged set.
  - Matching/deduplication uses normalized in-memory tokens only.
  - Output VCF keeps original values as stored in source/baseline.
"""

import json
import os
import re
import sys
from collections import Counter, defaultdict
from copy import deepcopy

try:
    import vobject
except ImportError:
    print("ERROR: missing dependency 'vobject'. Install with: python -m pip install vobject")
    sys.exit(1)


# Fields we do not copy from enrichment sources onto an existing matched contact.
# For brand-new missing contacts, we keep the source card structure, except for
# redundant per-contact duplicate fields removed by the merge cleanup logic.
SYSTEM_FIELDS = {"UID", "REV", "PRODID", "VERSION"}
NAME_FIELDS = {"FN", "N"}

# Standard-ish/user-facing vCard fields. Used only as a tie-breaker when the
# baseline does not already contain either field type.
STANDARD_FIELDS = {
    "FN", "N", "NICKNAME", "TEL", "EMAIL", "ADR", "URL", "BDAY", "ANNIVERSARY",
    "ORG", "TITLE", "ROLE", "NOTE", "PHOTO", "IMPP", "SOCIALPROFILE", "CATEGORIES",
    "GENDER", "LANG", "GEO", "TZ", "RELATED", "KIND", "SOURCE",
}

# Fields that are usually Apple helper metadata for grouped properties. These are
# allowed, but we try not to add them as orphaned fields onto matched contacts.
GROUP_HELPER_FIELDS = {"X-ABLABEL"}


class SourceStats:
    def __init__(self, source_name, source_path, source_contacts):
        self.source_name = source_name
        self.source_path = source_path
        self.source_contacts = source_contacts

        self.matched_contacts = 0
        self.missing_contacts_added = 0
        self.ignored_contacts_without_identifiers = 0
        self.ambiguous_matches = 0

        self.fields_added_to_matched = 0
        self.fields_included_in_added_contacts = 0
        self.skipped_duplicate_or_less_preferred_fields = 0
        self.field_upgrades = 0
        self.redundant_fields = 0
        self.contacts_with_skipped_additions = 0

        self.source_duplicate_identity_tokens = 0
        self.source_contacts_with_duplicate_identity = 0

        self.added_field_types = Counter()
        self.added_nonbaseline_field_types = Counter()
        self.skipped_field_types = Counter()
        self.skipped_nonbaseline_field_types = Counter()
        self.redundant_field_types = Counter()
        self.redundant_nonbaseline_field_types = Counter()
        self.discovered_nonbaseline_field_types = Counter()

        self.ignored_contact_names = []


# ----------------------------- basic parsing -----------------------------

def read_vcf(path):
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        return list(vobject.readComponents(f.read()))


def field_name(child):
    return str(child.name).upper()


def child_value(child):
    try:
        return str(child.value).strip()
    except Exception:
        return ""


def get_group(child):
    return getattr(child, "group", None)


def get_fn(card):
    if hasattr(card, "fn") and str(card.fn.value).strip():
        return str(card.fn.value).strip()
    if hasattr(card, "n") and str(card.n.value).strip():
        return str(card.n.value).strip()
    # Fall back to first email/phone-like value for reports.
    for child in card.getChildren():
        f = field_name(child)
        if is_email_like_field(f) or is_phone_like_field(f):
            v = child_value(child)
            if v:
                return v
    return "Unknown"


def ensure_fn(card):
    if hasattr(card, "fn") and str(card.fn.value).strip():
        return

    fallback = None
    if hasattr(card, "n") and str(card.n.value).strip():
        fallback = str(card.n.value).strip()
    else:
        for child in card.getChildren():
            if field_name(child) in {"EMAIL", "TEL"}:
                value = child_value(child)
                if value:
                    fallback = value
                    break

    if not fallback:
        fallback = "Unknown Contact"

    card.add("fn")
    card.fn.value = fallback


def count_fields(cards):
    counts = Counter()
    for card in cards:
        for child in card.getChildren():
            counts[field_name(child)] += 1
    return counts


# ----------------------------- normalization -----------------------------

def norm_phone(value):
    return re.sub(r"\D+", "", value or "")


def norm_email(value):
    return (value or "").strip().lower()


def norm_text(value):
    return re.sub(r"\s+", " ", str(value or "").strip().lower())


def is_phone_like_field(f):
    f = f.upper()
    return f == "TEL" or "PHONE" in f


def is_email_like_field(f):
    f = f.upper()
    return f == "EMAIL" or "EMAIL" in f


def is_social_like_field(f):
    f = f.upper()
    return f in {"IMPP", "SOCIALPROFILE", "X-SOCIALPROFILE"} or "SOCIAL" in f


def params_text(child):
    parts = []
    try:
        for k, v in child.params.items():
            if isinstance(v, list):
                vv = ",".join(str(x) for x in v)
            else:
                vv = str(v)
            parts.append(f"{k.upper()}={vv.upper()}")
    except Exception:
        pass
    return " ".join(parts)


def is_identity_capable_child(child):
    f = field_name(child)
    p = params_text(child)
    value = child_value(child)
    if not value:
        return False

    if is_phone_like_field(f) or is_email_like_field(f) or is_social_like_field(f):
        return True

    # A few iOS exports represent app identities as URLs or custom labels.
    # Use these as identity only when the field/params clearly indicate an app/social handle.
    marker_text = f"{f} {p}".upper()
    return any(marker in marker_text for marker in ["TELEGRAM", "SIGNAL", "INSTAGRAM", "WHATSAPP", "FACEBOOK", "TWITTER", "MASTODON"])


def comparable_token(child):
    """Token used for duplicate detection. Never written to output."""
    f = field_name(child)
    value = child_value(child)
    if not value:
        return ""

    if is_phone_like_field(f):
        phone = norm_phone(value)
        return f"phone:{phone}" if phone else ""

    if is_email_like_field(f):
        email = norm_email(value)
        return f"email:{email}" if email else ""

    if is_social_like_field(f):
        txt = norm_text(value)
        return f"social:{txt}" if txt else ""

    # URL may be a social/profile URL, but generic homepages are often shared by companies.
    # For duplicate detection inside the same/matched contact it is still useful as text.
    txt = norm_text(value)
    return f"text:{txt}" if txt else ""


def identity_tokens(card):
    tokens = set()
    for child in card.getChildren():
        if is_identity_capable_child(child):
            token = comparable_token(child)
            if token:
                tokens.add(token)
    return tokens


def has_identifier(card):
    return bool(identity_tokens(card))


# ----------------------------- priority/dedup -----------------------------

def field_priority(child, baseline_fields):
    """Lower is better."""
    f = field_name(child)
    if f in baseline_fields:
        return 0
    if f in STANDARD_FIELDS:
        return 1
    if f.startswith("X-"):
        return 3
    return 2


def better_child(a, b, baseline_fields):
    """Return the better child for representing the same comparable token."""
    pa = field_priority(a, baseline_fields)
    pb = field_priority(b, baseline_fields)
    if pa != pb:
        return a if pa < pb else b

    # Prefer non-empty/raw longer value only as a stable tie-breaker.
    va = child_value(a)
    vb = child_value(b)
    if len(va) != len(vb):
        return a if len(va) > len(vb) else b
    return a


def existing_token_map(card):
    result = defaultdict(list)
    for child in card.getChildren():
        token = comparable_token(child)
        if token:
            result[token].append(child)
    return result


def remove_child(card, child):
    key = field_name(child).lower()
    if key in card.contents:
        card.contents[key] = [c for c in card.contents[key] if c is not child]
        if not card.contents[key]:
            del card.contents[key]


def dedupe_children_for_contact(children, baseline_fields):
    """Return winners and redundant children within a single source contact.

    If a source contact has both TEL and X-PHONE with the same phone value,
    only the preferred representation is used. The others are redundant.
    """
    by_token = defaultdict(list)
    no_token = []

    for child in children:
        token = comparable_token(child)
        if token:
            by_token[token].append(child)
        else:
            no_token.append(child)

    winners = []
    redundant = []

    for _token, items in by_token.items():
        best = items[0]
        for item in items[1:]:
            best = better_child(best, item, baseline_fields)
        winners.append(best)
        for item in items:
            if item is not best:
                redundant.append(item)

    winners.extend(no_token)
    return winners, redundant


def build_clean_new_card(source_card, baseline_fields):
    """Deep-copy a missing contact and remove redundant fields inside it.

    The original source card is still used for missing_contacts.json.
    """
    new_card = deepcopy(source_card)
    children = list(new_card.getChildren())
    _winners, redundant = dedupe_children_for_contact(children, baseline_fields)
    for child in redundant:
        remove_child(new_card, child)
    ensure_fn(new_card)
    return new_card, redundant


def card_to_simple_json(card):
    values = defaultdict(list)
    for child in card.getChildren():
        f = field_name(child)
        v = child_value(child)
        if v:
            values[f].append(v)

    simplified = {}
    for f in sorted(values):
        unique_values = []
        seen = set()
        for v in values[f]:
            if v not in seen:
                unique_values.append(v)
                seen.add(v)
        simplified[f] = unique_values[0] if len(unique_values) == 1 else unique_values
    return simplified


# ----------------------------- matching/indexes -----------------------------

def build_token_index(cards):
    token_index = defaultdict(list)
    for card in cards:
        for token in identity_tokens(card):
            token_index[token].append(card)
    return token_index


def add_card_to_token_index(card, token_index):
    for token in identity_tokens(card):
        if card not in token_index[token]:
            token_index[token].append(card)


def find_match(card, token_index):
    candidates = []
    seen_ids = set()
    for token in identity_tokens(card):
        for candidate in token_index.get(token, []):
            cid = id(candidate)
            if cid not in seen_ids:
                candidates.append(candidate)
                seen_ids.add(cid)

    if not candidates:
        return None, False
    if len(candidates) == 1:
        return candidates[0], False

    # Deterministic choice: candidate with the most shared identity tokens.
    source_tokens = identity_tokens(card)
    candidates.sort(key=lambda c: len(source_tokens & identity_tokens(c)), reverse=True)
    return candidates[0], True


def source_duplicate_stats(cards):
    token_to_contact_indexes = defaultdict(set)
    for idx, card in enumerate(cards):
        for token in identity_tokens(card):
            token_to_contact_indexes[token].add(idx)

    duplicate_tokens = {t: idxs for t, idxs in token_to_contact_indexes.items() if len(idxs) > 1}
    duplicate_contact_indexes = set()
    for idxs in duplicate_tokens.values():
        duplicate_contact_indexes.update(idxs)
    return len(duplicate_tokens), len(duplicate_contact_indexes)


# ----------------------------- merge logic -----------------------------

def record_counter(counter, child):
    counter[field_name(child)] += 1


def track_nonbaseline_discovery(stats, source_cards, baseline_fields):
    for card in source_cards:
        for child in card.getChildren():
            f = field_name(child)
            if f not in baseline_fields:
                stats.discovered_nonbaseline_field_types[f] += 1


def maybe_record_nonbaseline(counter, child, baseline_fields):
    f = field_name(child)
    if f not in baseline_fields:
        counter[f] += 1


def merge_into_matched_contact(base_card, source_card, baseline_fields, generated_card_ids, stats):
    had_skipped = False
    existing = existing_token_map(base_card)

    # Dedupe within source contact first, so TEL beats X-PHONE before touching baseline.
    source_children = [
        c for c in source_card.getChildren()
        if field_name(c) not in SYSTEM_FIELDS and field_name(c) not in NAME_FIELDS
    ]
    source_winners, source_redundant = dedupe_children_for_contact(source_children, baseline_fields)

    for child in source_redundant:
        stats.redundant_fields += 1
        record_counter(stats.redundant_field_types, child)
        maybe_record_nonbaseline(stats.redundant_nonbaseline_field_types, child, baseline_fields)
        had_skipped = True

    # Avoid orphan Apple labels on matched contacts unless their group has a non-helper winner.
    groups_with_nonhelper_winner = {
        get_group(c) for c in source_winners
        if get_group(c) and field_name(c) not in GROUP_HELPER_FIELDS
    }

    for child in source_winners:
        f = field_name(child)
        if f in GROUP_HELPER_FIELDS and get_group(child) and get_group(child) not in groups_with_nonhelper_winner:
            # The corresponding grouped TEL/URL/etc. did not survive source-side dedupe.
            stats.skipped_duplicate_or_less_preferred_fields += 1
            record_counter(stats.skipped_field_types, child)
            maybe_record_nonbaseline(stats.skipped_nonbaseline_field_types, child, baseline_fields)
            had_skipped = True
            continue

        token = comparable_token(child)
        if not token:
            continue

        if token not in existing:
            base_card.add(deepcopy(child))
            existing[token].append(child)
            stats.fields_added_to_matched += 1
            record_counter(stats.added_field_types, child)
            maybe_record_nonbaseline(stats.added_nonbaseline_field_types, child, baseline_fields)
            continue

        # Value already exists on this contact. Baseline-origin contacts are immutable;
        # generated contacts may be upgraded from custom/non-baseline to preferred fields.
        existing_items = existing[token]
        best_existing = existing_items[0]
        for item in existing_items[1:]:
            best_existing = better_child(best_existing, item, baseline_fields)

        if id(base_card) in generated_card_ids:
            best_after = better_child(best_existing, child, baseline_fields)
            if best_after is child and field_priority(child, baseline_fields) < field_priority(best_existing, baseline_fields):
                # Upgrade: add better source representation, remove worse existing representations.
                base_card.add(deepcopy(child))
                for old in list(existing_items):
                    if field_priority(old, baseline_fields) > field_priority(child, baseline_fields):
                        remove_child(base_card, old)
                        stats.field_upgrades += 1
                        stats.redundant_fields += 1
                        record_counter(stats.redundant_field_types, old)
                        maybe_record_nonbaseline(stats.redundant_nonbaseline_field_types, old, baseline_fields)
                existing = existing_token_map(base_card)
                record_counter(stats.added_field_types, child)
                maybe_record_nonbaseline(stats.added_nonbaseline_field_types, child, baseline_fields)
                continue

        # Duplicate or not better.
        stats.skipped_duplicate_or_less_preferred_fields += 1
        record_counter(stats.skipped_field_types, child)
        maybe_record_nonbaseline(stats.skipped_nonbaseline_field_types, child, baseline_fields)
        had_skipped = True

    if had_skipped:
        stats.contacts_with_skipped_additions += 1


def process_source(source_path, base_cards, token_index, baseline_fields, generated_card_ids, missing_contacts_report):
    source_name = os.path.basename(source_path)
    source_cards = read_vcf(source_path)
    stats = SourceStats(source_name, source_path, len(source_cards))

    dup_tokens, dup_contacts = source_duplicate_stats(source_cards)
    stats.source_duplicate_identity_tokens = dup_tokens
    stats.source_contacts_with_duplicate_identity = dup_contacts

    track_nonbaseline_discovery(stats, source_cards, baseline_fields)
    missing_contacts_report.setdefault(source_name, [])

    for source_card in source_cards:
        if not has_identifier(source_card):
            stats.ignored_contacts_without_identifiers += 1
            if len(stats.ignored_contact_names) < 50:
                stats.ignored_contact_names.append(get_fn(source_card))
            continue

        match, ambiguous = find_match(source_card, token_index)
        if ambiguous:
            stats.ambiguous_matches += 1

        if match is not None:
            stats.matched_contacts += 1
            merge_into_matched_contact(match, source_card, baseline_fields, generated_card_ids, stats)
            # Fields added to an existing contact may introduce new identity tokens.
            add_card_to_token_index(match, token_index)
            continue

        # Missing contact: keep a JSON audit of the full original source card,
        # but add a cleaned/deduped VCF card to the merged output.
        new_card, redundant = build_clean_new_card(source_card, baseline_fields)

        for child in redundant:
            stats.redundant_fields += 1
            record_counter(stats.redundant_field_types, child)
            maybe_record_nonbaseline(stats.redundant_nonbaseline_field_types, child, baseline_fields)

        base_cards.append(new_card)
        generated_card_ids.add(id(new_card))
        add_card_to_token_index(new_card, token_index)

        stats.missing_contacts_added += 1
        missing_contacts_report[source_name].append(card_to_simple_json(source_card))

        # Count all fields included in the cleaned new card except system fields.
        for child in new_card.getChildren():
            if field_name(child) in SYSTEM_FIELDS:
                continue
            stats.fields_included_in_added_contacts += 1
            record_counter(stats.added_field_types, child)
            maybe_record_nonbaseline(stats.added_nonbaseline_field_types, child, baseline_fields)

    return stats


# ----------------------------- output/report -----------------------------

def serialize_cards(cards, output_path):
    with open(output_path, "w", encoding="utf-8", newline="") as f:
        for card in cards:
            ensure_fn(card)
            serialized = card.serialize(validate=False)
            f.write(serialized)
            if not serialized.endswith("\n"):
                f.write("\n")


def write_missing_json(missing_contacts_report, output_path):
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(missing_contacts_report, f, ensure_ascii=False, indent=2)


def format_counter_lines(counter, indent="  "):
    if not counter:
        return [f"{indent}(none)"]
    return [f"{indent}{key}: {counter[key]}" for key in sorted(counter)]


def format_set_lines(values, indent="  "):
    if not values:
        return [f"{indent}(none)"]
    return [f"{indent}{v}" for v in sorted(values)]


def write_report(report_path, baseline_path, baseline_counts, original_baseline_count, final_cards, existing_sources, skipped_source_paths, stats_list, generated_card_ids):
    final_counts = count_fields(final_cards)
    baseline_fields = set(baseline_counts.keys())
    final_nonbaseline_counts = Counter({k: v for k, v in final_counts.items() if k not in baseline_fields})

    overall_discovered_nonbaseline = Counter()
    overall_added_nonbaseline = Counter()
    overall_skipped_nonbaseline = Counter()
    overall_redundant_nonbaseline = Counter()

    for st in stats_list:
        overall_discovered_nonbaseline.update(st.discovered_nonbaseline_field_types)
        overall_added_nonbaseline.update(st.added_nonbaseline_field_types)
        overall_skipped_nonbaseline.update(st.skipped_nonbaseline_field_types)
        overall_redundant_nonbaseline.update(st.redundant_nonbaseline_field_types)

    total_missing_added = sum(st.missing_contacts_added for st in stats_list)
    total_ignored = sum(st.ignored_contacts_without_identifiers for st in stats_list)
    total_ambiguous = sum(st.ambiguous_matches for st in stats_list)
    total_upgrades = sum(st.field_upgrades for st in stats_list)

    lines = []
    lines.append("VCF Merge Report")
    lines.append("================")
    lines.append("")
    lines.append(f"Baseline file: {os.path.basename(baseline_path)}")
    lines.append(f"Baseline path: {baseline_path}")
    lines.append(f"Baseline contacts: {original_baseline_count}")
    lines.append(f"Final contacts: {len(final_cards)}")
    lines.append(f"Missing contacts added: {total_missing_added}")
    lines.append(f"Ignored contacts without identifiers: {total_ignored}")
    lines.append(f"Ambiguous matches encountered: {total_ambiguous}")
    lines.append(f"Field upgrades performed: {total_upgrades}")
    lines.append("")

    lines.append("Baseline fields:")
    lines.extend(format_counter_lines(baseline_counts))
    lines.append("")

    if skipped_source_paths:
        lines.append("Skipped source files:")
        for p in skipped_source_paths:
            lines.append(f"  {p}")
        lines.append("")

    if not existing_sources:
        lines.append("No existing secondary VCF files found. Nothing to update.")
        lines.append("")

    for st in stats_list:
        lines.append(f"Source: {st.source_name}")
        lines.append("-" * (8 + len(st.source_name)))
        lines.append(f"Path: {st.source_path}")
        lines.append(f"Source contacts: {st.source_contacts}")
        lines.append(f"Matched contacts: {st.matched_contacts}")
        lines.append(f"Missing contacts added: {st.missing_contacts_added}")
        lines.append(f"Ignored contacts without identifiers: {st.ignored_contacts_without_identifiers}")
        lines.append(f"Ambiguous matches encountered: {st.ambiguous_matches}")
        lines.append(f"Duplicate identity tokens inside source: {st.source_duplicate_identity_tokens}")
        lines.append(f"Contacts sharing duplicate source identity tokens: {st.source_contacts_with_duplicate_identity}")
        lines.append("")

        lines.append(f"Fields added to matched contacts: {st.fields_added_to_matched}")
        lines.append(f"Fields included in added contacts: {st.fields_included_in_added_contacts}")
        lines.append(f"Skipped duplicate/less-preferred fields: {st.skipped_duplicate_or_less_preferred_fields}")
        lines.append(f"Redundant fields removed/ignored: {st.redundant_fields}")
        lines.append(f"Field upgrades: {st.field_upgrades}")
        lines.append(f"Contacts with skipped/redundant additions: {st.contacts_with_skipped_additions}")
        lines.append("")

        lines.append("Non-baseline field types discovered in this source:")
        lines.extend(format_counter_lines(st.discovered_nonbaseline_field_types))
        lines.append("")
        lines.append("Non-baseline field types accepted from this source:")
        lines.extend(format_counter_lines(st.added_nonbaseline_field_types))
        lines.append("")
        lines.append("Non-baseline field types made redundant in this source:")
        lines.extend(format_counter_lines(st.redundant_nonbaseline_field_types))
        lines.append("")
        lines.append("Non-baseline field types skipped in this source:")
        lines.extend(format_counter_lines(st.skipped_nonbaseline_field_types))
        lines.append("")

        if st.ignored_contact_names:
            lines.append("Ignored contacts without identifiers, first 50:")
            for name in st.ignored_contact_names:
                lines.append(f"  {name}")
            lines.append("")

    lines.append("Overall non-baseline field summary")
    lines.append("----------------------------------")
    lines.append("Discovered non-baseline field types:")
    lines.extend(format_counter_lines(overall_discovered_nonbaseline))
    lines.append("")
    lines.append("Non-baseline field types present in final output:")
    lines.extend(format_counter_lines(final_nonbaseline_counts))
    lines.append("")
    lines.append("Non-baseline field types accepted during merge:")
    lines.extend(format_counter_lines(overall_added_nonbaseline))
    lines.append("")
    lines.append("Non-baseline field types made redundant during merge:")
    lines.extend(format_counter_lines(overall_redundant_nonbaseline))
    lines.append("")
    lines.append("Non-baseline field types skipped during merge:")
    lines.extend(format_counter_lines(overall_skipped_nonbaseline))
    lines.append("")

    lines.append("Output files:")
    lines.append("  merged_contacts.vcf")
    lines.append("  missing_contacts.json")
    lines.append("  merge_report.txt")
    lines.append("")

    with open(report_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))


# ----------------------------- main -----------------------------

def main():
    if len(sys.argv) < 2:
        print("ERROR: No baseline VCF specified.")
        print("Usage: python contacts_merge_v2.py baseline.vcf [source1.vcf source2.vcf ...]")
        sys.exit(2)

    baseline_path = sys.argv[1]
    source_paths = sys.argv[2:]

    if not os.path.exists(baseline_path):
        print(f"ERROR: Baseline VCF does not exist: {baseline_path}")
        sys.exit(2)

    existing_sources = []
    skipped_sources = []
    for p in source_paths:
        if os.path.exists(p):
            existing_sources.append(p)
        else:
            skipped_sources.append(p)
            print(f"WARNING: Source file skipped because it does not exist: {p}")

    base_cards = read_vcf(baseline_path)
    original_baseline_count = len(base_cards)
    baseline_counts = count_fields(base_cards)
    baseline_fields = set(baseline_counts.keys())

    print(f"Baseline contacts: {original_baseline_count}")
    print("Baseline fields:")
    for line in format_counter_lines(baseline_counts):
        print(line)

    if not existing_sources:
        print("No existing secondary VCF files found. Nothing to update.")

    token_index = build_token_index(base_cards)
    generated_card_ids = set()
    missing_contacts_report = {}
    stats_list = []

    for source_path in existing_sources:
        print(f"Processing source: {source_path}")
        stats = process_source(
            source_path=source_path,
            base_cards=base_cards,
            token_index=token_index,
            baseline_fields=baseline_fields,
            generated_card_ids=generated_card_ids,
            missing_contacts_report=missing_contacts_report,
        )
        stats_list.append(stats)

    serialize_cards(base_cards, "merged_contacts.vcf")
    write_missing_json(missing_contacts_report, "missing_contacts.json")
    write_report(
        report_path="merge_report.txt",
        baseline_path=baseline_path,
        baseline_counts=baseline_counts,
        original_baseline_count=original_baseline_count,
        final_cards=base_cards,
        existing_sources=existing_sources,
        skipped_source_paths=skipped_sources,
        stats_list=stats_list,
        generated_card_ids=generated_card_ids,
    )

    print("Done.")
    print(f"Final contacts: {len(base_cards)}")
    print("Created:")
    print("  merged_contacts.vcf")
    print("  missing_contacts.json")
    print("  merge_report.txt")


if __name__ == "__main__":
    main()
