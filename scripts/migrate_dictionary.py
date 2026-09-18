"""Migrate the legacy medical knowledge files into one master dictionary.

Reads the five legacy rule files (each in its own tier) plus the curated
Speechmatics vocabulary, and writes ``medical_knowledge/medical_dictionary.json``
(the runtime source of truth) and regenerates
``medical_knowledge/speechmatics_additional_vocab.json`` from the
``speechmatics: true`` entries (§9: a separate, bounded generation step).

Source file -> tier mapping (§9):
    fst_terms.json            -> curated   (unit-level rules -> unit)
    abbreviations.json        -> abbreviation
    observed_asr_aliases.json -> observed_alias
    nursing_phrases.json      -> phrase
    nursing_terms.json        -> validated_term

Migration rules:
-   Every legacy (form, canonical) pair survives: forms are merged per
    canonical into one term; a form that lost a cross-canonical conflict
    STAYS listed in the losing term so no knowledge is dropped - the loader
    re-resolves the conflict at startup and reports it (same warnings as
    the legacy loader emitted).
-   The term's tier/source_file reproduce the legacy winner for its forms:
    the highest-priority legacy rule that survived dedup for that canonical.
-   Vocabulary: a term is ``speechmatics: true`` exactly when its canonical
    is in the shipped curated vocab (sounds_like copied verbatim). Vocab
    entries whose canonical is not a rule term become vocab-only terms
    (``forms: []``) so the derived vocab never balloons past what shipped.

Usage:
    python scripts/migrate_dictionary.py            # write + report
    python scripts/migrate_dictionary.py --check    # verify sync, change nothing
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import OrderedDict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from speechmatics_test.text import normalize_text  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
KNOWLEDGE = ROOT / "medical_knowledge"

TIER_ORDER = ("curated", "abbreviation", "observed_alias", "phrase",
              "validated_term", "unit")
TIER_RANK = {name: rank for rank, name in enumerate(TIER_ORDER)}

#: Legacy source -> tier for its rules (§9). fst_terms unit rules are
#: overridden to "unit" below: units keep today's tier semantics.
SOURCE_TIER = {
    "fst_terms.json": "curated",
    "abbreviations.json": "abbreviation",
    "observed_asr_aliases.json": "observed_alias",
    "nursing_phrases.json": "phrase",
    "nursing_terms.json": "validated_term",
}

#: Curated category overrides where the mechanical rule would be too coarse
#: (validated/observed canonicals whose clinical category is known).
TYPE_OVERRIDES = {
    "cyst": "condition", "cystic": "condition", "hemorrhagic": "condition",
    "lesion": "condition", "hypertension": "condition", "diabetes": "condition",
    "diabetes mellitus": "condition", "pneumonia": "condition",
    "sepsis": "condition", "myocardial infarction": "condition",
    "heart failure": "condition", "asthma": "condition",
    "right lung": "anatomy", "left lung": "anatomy", "C3-C4": "anatomy",
    "coronary artery": "anatomy", "liver": "anatomy", "kidney": "anatomy",
    "ultrasound": "imaging", "echocardiogram": "imaging", "X-ray": "imaging",
    "MRI": "imaging", "CT scan": "imaging",
    "Mg": "lab", "Magnesium": "lab", "HbA1c": "lab", "hemoglobin": "lab",
    "creatinine": "lab", "troponin": "lab", "glucose": "lab",
    "sodium": "lab", "potassium": "lab", "blood culture": "lab",
    "intubation": "procedure", "catheterization": "procedure",
    "biopsy": "procedure", "dialysis": "procedure",
    "acetaminophen": "drug", "metformin": "drug", "insulin": "drug",
    "amoxicillin": "drug", "ceftriaxone": "drug", "vancomycin": "drug",
    "heparin": "drug", "warfarin": "drug", "furosemide": "drug",
    "ondansetron": "drug",
    "O2": "vital_sign", "heart rate": "vital_sign",
    "blood pressure": "vital_sign", "oxygen saturation": "vital_sign",
    "intravenous": "route", "intramuscular": "route", "subcutaneous": "route",
    "CCU": "abbreviation", "ICU": "abbreviation",
}

#: Legacy load order (the legacy loader's dedup/winner order).
LEGACY_SOURCES = ("fst_terms.json", "observed_asr_aliases.json",
                  "abbreviations.json", "nursing_phrases.json",
                  "nursing_terms.json")

#: Legacy tier numbering inside fst_terms.json's "tiers" map (the old loader's
#: within-file priority). Together with the group (fst_terms first) this is
#: the legacy resolution order the consolidated dictionary must reproduce.
LEGACY_TIER_INDEX = {"abbreviation": 0, "observed_alias": 1, "phrase": 2,
                     "validated_term": 3, "unit": 4}
SOURCE_GROUP = {name: 0 if name == "fst_terms.json" else 1
                for name in LEGACY_SOURCES}


def _read(name: str):
    return json.loads((KNOWLEDGE / name).read_text(encoding="utf-8"))


def extract_legacy_rules() -> list[dict]:
    """Legacy rules with both the new tier name and the legacy sort key.

    The legacy key (group, tier index) is the resolution order the old
    loader used; the migration must reproduce its winners exactly.
    """
    raw: list[tuple[str, str, str, str]] = []

    data = _read("fst_terms.json")
    for rule in data.get("rules", []):
        raw.append((str(rule.get("form", "")), str(rule.get("canonical", "")).strip(),
                    str(rule.get("tier", "validated_term")), "fst_terms.json"))

    data = _read("observed_asr_aliases.json")
    for canonical, info in data.items():
        for form in list(info.get("spoken_forms", [])) + list(info.get("common_asr_forms", [])):
            raw.append((str(form), str(canonical), "observed_alias",
                        "observed_asr_aliases.json"))

    data = _read("abbreviations.json")
    for canonical, info in data.items():
        for form in list(info.get("spoken", [])) + list(info.get("persian", [])):
            raw.append((str(form), str(info.get("canonical", canonical)),
                        "abbreviation", "abbreviations.json"))

    data = _read("nursing_phrases.json")
    for phrase in data:
        for form in phrase.get("spoken_forms", []):
            raw.append((str(form), str(phrase.get("canonical", "")),
                        "phrase", "nursing_phrases.json"))

    data = _read("nursing_terms.json")
    for entry in data.get("entries", []):
        for form in list(entry.get("spoken_forms", [])) + list(entry.get("common_asr_forms", [])):
            raw.append((str(form), str(entry.get("canonical", "")),
                        "validated_term", "nursing_terms.json"))

    rules = []
    for index, (form, canonical, tier, source) in enumerate(raw):
        form = normalize_text(form)
        canonical = canonical.strip()
        if not form or not canonical or form == canonical:
            continue
        if source == "fst_terms.json":
            # §9: unit-level entries keep the unit tier even though they come
            # from the (otherwise curated) fst_terms rule set.
            new_tier = "unit" if tier == "unit" else "curated"
        else:
            new_tier = SOURCE_TIER[source]
        legacy_key = (SOURCE_GROUP[source], LEGACY_TIER_INDEX[tier], index)
        rules.append({"form": form, "canonical": canonical,
                      "tier": new_tier, "legacy_tier": tier,
                      "source": source, "legacy_key": legacy_key})
    return rules


def build_terms(rules: list[dict], vocab: list) -> list[dict]:
    """Merge legacy rules per canonical into dictionary terms.

    A term's tier/source_file come from the legacy winner among its rules
    (best legacy key), so the consolidated dictionary keeps today's
    resolution semantics for the term's forms.
    """
    vocab_by_content = {}
    for item in vocab:
        if isinstance(item, dict):
            vocab_by_content[item["content"]] = item.get("sounds_like", [])
        else:
            vocab_by_content[item] = []

    terms: "OrderedDict[str, dict]" = OrderedDict()
    for rule in rules:
        canonical = rule["canonical"]
        term = terms.get(canonical)
        if term is None:
            term = terms[canonical] = {
                "canonical": canonical,
                "forms": [],
                "tier": rule["tier"],
                "source_file": rule["source"],
                "_best_key": rule["legacy_key"],
                "_first_seen": len(terms),
            }
        if rule["form"] not in term["forms"]:
            term["forms"].append(rule["form"])
        if rule["legacy_key"] < term["_best_key"]:
            term["_best_key"] = rule["legacy_key"]
            term["tier"] = rule["tier"]
            term["source_file"] = rule["source"]

    # vocab-only entries become terms with no matching forms
    for content in vocab_by_content:
        if content not in terms:
            terms[content] = {
                "canonical": content, "forms": [],
                "tier": "validated_term", "source_file": None,
                "_best_key": (99, 99, 99), "_first_seen": len(terms),
            }

    for canonical, entry in vocab_by_content.items():
        term = terms[canonical]
        term["speechmatics"] = True
        term["sounds_like"] = list(entry)

    for term in terms.values():
        term["type"] = _term_type(term)
        term["speechmatics"] = bool(term.get("speechmatics"))
        term.setdefault("sounds_like", [])
    return list(terms.values())


def _form_winners(rules: list[dict]) -> dict[str, dict]:
    """Legacy winner per form: the rule the old loader actually fired."""
    form_winner: dict[str, dict] = {}
    for rule in rules:
        best = form_winner.get(rule["form"])
        if best is None or rule["legacy_key"] < best["legacy_key"]:
            form_winner[rule["form"]] = rule
    return form_winner


def _drop_tier_flip_losers(terms: list[dict], form_winner: dict[str, dict]) -> list[str]:
    """Drop losing forms whose conflict the flat tier order would FLIP.

    The six-tier order is flat while the legacy order was (source group,
    tier). The two agree on every shipped conflict EXCEPT when a losing
    claim from a weak source was absorbed into a term whose own forms make
    it the same (or better) tier than the true winner - then "tier, then
    stable order" would pick the wrong canonical. Such a form never fired
    under the legacy loader, so it is removed from the losing term (the
    term itself, its other forms and the legacy file all survive) and the
    resolution is reported.
    """
    report: list[str] = []
    by_canonical = {t["canonical"]: t for t in terms}
    order = {id(t): pos for pos, t in enumerate(terms)}
    for term in terms:
        for form in list(term["forms"]):
            winner = form_winner.get(form)
            if winner is None or winner["canonical"] == term["canonical"]:
                continue
            winner_term = by_canonical[winner["canonical"]]
            # New-scheme resolution of this form: tier rank, then term order.
            if (TIER_RANK[term["tier"]], order[id(term)]) < \
                    (TIER_RANK[winner_term["tier"]], order[id(winner_term)]):
                term["forms"].remove(form)
                report.append(
                    f"dropped losing form {form!r} from term "
                    f"{term['canonical']!r} ({term['tier']}): under the flat tier "
                    f"order it would have wrongly beaten the legacy winner "
                    f"{winner['canonical']!r} ({winner_term['tier']}, "
                    f"{winner['source']}); legacy mapping stays in "
                    f"{term['source_file']}"
                )
    return report


def _term_type(term: dict) -> str:
    canonical = term["canonical"]
    if canonical in TYPE_OVERRIDES:
        return TYPE_OVERRIDES[canonical]
    tier = term["tier"]
    if tier == "unit":
        return "dosage_unit"
    if tier in ("curated", "abbreviation"):
        return "abbreviation"
    if tier == "phrase":
        return "phrase"
    return "term"


def _slugify(canonical: str) -> str:
    if canonical == "%":
        return "percent"
    slug = []
    for ch in canonical.lower():
        slug.append(ch if ch.isalnum() else "-")
    return "".join(slug).strip("-") or "term"


def assign_ids(terms: list[dict]) -> list[dict]:
    """Deterministic unique ids; a canonical that IS a slug keeps it first.

    Two-phase so case-only pairs resolve readably: canonical "mg" claims the
    id "mg" before "Mg" (whose slug is also "mg") is considered; "Mg" then
    falls back to its case-preserved canonical.
    """
    used: set[str] = set()
    phase1 = [t for t in terms if t["canonical"] == _slugify(t["canonical"])]
    phase2 = [t for t in terms if t not in phase1]
    for term in phase1 + phase2:
        slug = _slugify(term["canonical"])
        if slug not in used:
            term_id = slug
        elif term["canonical"] not in used:
            term_id = term["canonical"]
        else:
            term_id = f"{slug}-{term['type']}"
        if term_id in used:
            n = 2
            while f"{slug}-{n}" in used:
                n += 1
            term_id = f"{slug}-{n}"
        used.add(term_id)
        term["id"] = term_id
    return terms


def sort_terms(terms: list[dict]) -> list[dict]:
    """Priority-consistent deterministic order: tier rank, then first sight."""
    return sorted(terms, key=lambda t: (TIER_RANK[t["tier"]], t["_first_seen"]))


def dictionary_payload(terms: list[dict]) -> dict:
    ordered = []
    for term in terms:
        entry = {"id": term["id"], "canonical": term["canonical"],
                 "type": term["type"], "tier": term["tier"],
                 "forms": term["forms"], "speechmatics": term["speechmatics"]}
        if term.get("sounds_like"):
            entry["sounds_like"] = term["sounds_like"]
        if term.get("source_file"):
            entry["source_file"] = term["source_file"]
        ordered.append(entry)
    return {"version": 1, "terms": ordered}


def derive_vocab(terms: list[dict]) -> list:
    """Bounded additional_vocab from speechmatics-eligible terms only."""
    out = []
    for term in terms:
        if not term.get("speechmatics"):
            continue
        if term.get("sounds_like"):
            out.append({"content": term["canonical"],
                        "sounds_like": term["sounds_like"]})
        else:
            out.append(term["canonical"])
    return out


def migrate() -> tuple[dict, list, list[str]]:
    report: list[str] = []
    rules = extract_legacy_rules()
    report.append(f"legacy rules extracted (normalized, non-empty): {len(rules)}")

    vocab = _read("speechmatics_additional_vocab.json")
    terms = build_terms(rules, vocab)
    terms = sort_terms(terms)
    # Flip check must see the FINAL (tier, stable) order the loader will use.
    report.extend(_drop_tier_flip_losers(terms, _form_winners(rules)))
    terms = assign_ids(terms)
    payload = dictionary_payload(terms)

    n_matched = sum(1 for t in terms if t["forms"])
    n_vocab_only = sum(1 for t in terms if not t["forms"])
    report.append(f"terms: {len(terms)} (with forms: {n_matched}, "
                  f"vocab-only: {n_vocab_only})")
    report.append("vocab entries (speechmatics: true): "
                  f"{sum(1 for t in terms if t['speechmatics'])}")

    derived = derive_vocab(terms)
    shipped_contents = [v["content"] if isinstance(v, dict) else v for v in vocab]
    derived_contents = [v["content"] if isinstance(v, dict) else v for v in derived]
    if sorted(derived_contents) == sorted(shipped_contents) and \
            sorted(json.dumps(v, sort_keys=True, ensure_ascii=False) for v in derived) == \
            sorted(json.dumps(v, sort_keys=True, ensure_ascii=False) for v in vocab):
        report.append("vocab parity: derived additional_vocab is content-identical "
                      "to the shipped file (order may differ)")
    else:
        report.append("vocab parity: MISMATCH between derived and shipped vocab!")
        report.append(f"  only in derived: {sorted(set(derived_contents) - set(shipped_contents))}")
        report.append(f"  only in shipped: {sorted(set(shipped_contents) - set(derived_contents))}")
    return payload, derived, report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true",
                        help="verify the on-disk dictionary/vocab are in sync; write nothing")
    args = parser.parse_args()

    payload, derived, report = migrate()

    if args.check:
        on_disk = json.loads((KNOWLEDGE / "medical_dictionary.json").read_text(encoding="utf-8"))
        vocab_disk = json.loads((KNOWLEDGE / "speechmatics_additional_vocab.json").read_text(encoding="utf-8"))
        ok = True
        if on_disk != payload:
            ok = False
            report.append("CHECK FAILED: medical_dictionary.json differs from a "
                          "fresh migration of the legacy files")
        if sorted(json.dumps(v, sort_keys=True, ensure_ascii=False) for v in vocab_disk) != \
                sorted(json.dumps(v, sort_keys=True, ensure_ascii=False) for v in derived):
            ok = False
            report.append("CHECK FAILED: speechmatics_additional_vocab.json is not "
                          "the derived vocab of the dictionary")
        report.append("CHECK: OK" if ok else "CHECK: FAILED")
        print("\n".join(report))
        return 0 if ok else 1

    (KNOWLEDGE / "medical_dictionary.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    # Regenerate the shipped vocab artifact from the dictionary so the two can
    # never drift (content-identical to the legacy file, order follows terms).
    (KNOWLEDGE / "speechmatics_additional_vocab.json").write_text(
        json.dumps(derived, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print("\n".join(report))
    print(f"\nwrote: {KNOWLEDGE / 'medical_dictionary.json'}")
    print(f"regenerated: {KNOWLEDGE / 'speechmatics_additional_vocab.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
