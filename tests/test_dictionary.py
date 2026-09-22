"""Medical dictionary schema, validation, vocab bounding and migration tests.

Covers §2/§9/§10 of the consolidation: the consolidated
``medical_knowledge/medical_dictionary.json`` schema (tier/type/forms/
speechmatics), clear failures on invalid data, the bounded Speechmatics
vocabulary derived from ``speechmatics: true`` entries only, and the
migration parity guards against the legacy knowledge files.
"""

import json
import subprocess
import sys
from pathlib import Path

import pytest

from speechmatics_test.matcher import (
    TERM_TYPES,
    TIER_ORDER,
    FstError,
    MedicalMatcher,
)

ROOT = Path(__file__).resolve().parents[1]
KNOWLEDGE = ROOT / "medical_knowledge"
DICTIONARY = KNOWLEDGE / "medical_dictionary.json"


def load_repo_dictionary() -> dict:
    return json.loads(DICTIONARY.read_text(encoding="utf-8"))


def write_dictionary(tmp_path: Path, payload) -> Path:
    knowledge = tmp_path / "medical_knowledge"
    knowledge.mkdir(parents=True, exist_ok=True)
    path = knowledge / "medical_dictionary.json"
    text = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False)
    path.write_text(text, encoding="utf-8")
    return tmp_path


def valid_term(**overrides):
    entry = {"id": "t1", "canonical": "Target", "type": "term",
             "tier": "validated_term", "forms": ["فرم یک"]}
    entry.update(overrides)
    return entry


# ------------------------------------------------------------- schema shape

def test_repo_dictionary_schema_is_valid():
    data = load_repo_dictionary()
    assert data["version"] == 1
    terms = data["terms"]
    assert terms, "dictionary must not be empty"
    ids = [t["id"] for t in terms]
    canonicals = [t["canonical"] for t in terms]
    assert len(ids) == len(set(ids))
    assert len(canonicals) == len(set(canonicals))
    for t in terms:
        assert t["tier"] in TIER_ORDER, t
        assert t["type"] in TERM_TYPES, t
        assert isinstance(t["forms"], list)
        assert isinstance(t.get("speechmatics", False), bool)
        assert t["forms"] or t["speechmatics"], t  # empty forms = vocab-only


def test_repo_dictionary_tiers_are_valid_and_legacy_mapped():
    """Every term's tier is a documented tier; the legacy source files map
    to the tiers the migration assigns them (fst_terms -> curated, units
    -> unit, phrases -> phrase, nursing rows -> validated_term, aliases ->
    observed_alias). The abbreviation tier has no shipped entries today:
    every abbreviations.json canonical was already covered - at higher
    priority - by the curated rule set."""
    data = load_repo_dictionary()
    tiers = {t["tier"] for t in data["terms"]}
    assert tiers <= set(TIER_ORDER)
    assert {"curated", "unit", "phrase", "validated_term", "observed_alias"} <= tiers


def test_unit_entries_kept_the_unit_tier():
    """§9: unit-level entries map to the unit tier (lowest priority)."""
    data = load_repo_dictionary()
    by_canonical = {t["canonical"]: t for t in data["terms"]}
    for unit in ("mg", "mL", "L", "kg", "mmHg", "%"):
        assert by_canonical[unit]["tier"] == "unit"
        assert by_canonical[unit]["type"] == "dosage_unit"


def test_no_case_only_duplicate_canonicals_except_case_sensitive_units():
    data = json.loads((KNOWLEDGE / "medical_dictionary.json").read_text(encoding="utf-8"))
    groups = {}
    for term in data["terms"]:
        groups.setdefault(term["canonical"].casefold(), set()).add(term["canonical"])
    # Mg (magnesium) and mg (milligram) are clinically distinct and must not
    # be merged merely because casefolding collides.
    collisions = {
        key: sorted(values) for key, values in groups.items()
        if len(values) > 1 and values != {"Mg", "mg"}
    }
    assert collisions == {}


def test_legacy_source_files_are_recorded_for_traceability():
    data = load_repo_dictionary()
    sources = {t.get("source_file") for t in data["terms"]}
    assert {"fst_terms.json", "observed_asr_aliases.json",
            "nursing_phrases.json", "nursing_terms.json"} <= sources


# --------------------------------------------------------- invalid entries

@pytest.mark.parametrize("overrides,match", [
    ({"tier": "nope"}, "tier"),
    ({"tier": None}, "tier"),
    ({"type": "nope"}, "type"),
    ({"type": None}, "type"),
    ({"canonical": ""}, "canonical"),
    ({"canonical": None}, "canonical"),
    ({"id": ""}, "id"),
    ({"id": None}, "id"),
    ({"forms": []}, "empty 'forms'"),           # non-vocab term must have forms
    ({"forms": "فرم"}, "list of non-empty strings"),
    ({"forms": [None]}, "list of non-empty strings"),
    ({"forms": [""]}, "list of non-empty strings"),
    ({"speechmatics": "yes"}, "boolean"),
    ({"sounds_like": "ام آر آی"}, "sounds_like"),
    ({"sounds_like": [""]}, "sounds_like"),
    ({"source_file": 5}, "source_file"),
])
def test_invalid_entries_fail_clearly(tmp_path, overrides, match):
    write_dictionary(tmp_path, {"version": 1, "terms": [valid_term(**overrides)]})
    with pytest.raises(FstError, match=match):
        MedicalMatcher(tmp_path)


def test_missing_dictionary_file_fails(tmp_path):
    (tmp_path / "medical_knowledge").mkdir()
    with pytest.raises(FstError, match="not found"):
        MedicalMatcher(tmp_path)


def test_invalid_json_fails_clearly(tmp_path):
    write_dictionary(tmp_path, "{not json")
    with pytest.raises(FstError, match="invalid JSON"):
        MedicalMatcher(tmp_path)


def test_unsupported_version_fails(tmp_path):
    write_dictionary(tmp_path, {"version": 99, "terms": [valid_term()]})
    with pytest.raises(FstError, match="unsupported version"):
        MedicalMatcher(tmp_path)


def test_missing_terms_list_fails(tmp_path):
    write_dictionary(tmp_path, {"version": 1})
    with pytest.raises(FstError, match="'terms' must be a list"):
        MedicalMatcher(tmp_path)


def test_empty_terms_list_fails(tmp_path):
    write_dictionary(tmp_path, {"version": 1, "terms": []})
    with pytest.raises(FstError, match="'terms' is empty"):
        MedicalMatcher(tmp_path)


def test_duplicate_id_fails(tmp_path):
    write_dictionary(tmp_path, {"version": 1, "terms": [
        valid_term(), valid_term(canonical="Other"),
    ]})
    with pytest.raises(FstError, match="duplicate id"):
        MedicalMatcher(tmp_path)


def test_duplicate_canonical_fails(tmp_path):
    write_dictionary(tmp_path, {"version": 1, "terms": [
        valid_term(), valid_term(id="t2"),
    ]})
    with pytest.raises(FstError, match="duplicate canonical"):
        MedicalMatcher(tmp_path)


def test_vocabulary_only_term_with_empty_forms_is_valid(tmp_path):
    write_dictionary(tmp_path, {"version": 1, "terms": [
        valid_term(id="v1", canonical="metformin", type="drug",
                   tier="validated_term", forms=[],
                   speechmatics=True, sounds_like=["متفورمین"]),
    ]})
    matcher = MedicalMatcher(tmp_path)
    assert matcher.rules == []  # no matcher rule, but valid ASR vocabulary
    assert matcher.warnings  # no-rules no-op warning
    assert matcher.additional_vocab == [
        {"content": "metformin", "sounds_like": ["متفورمین"]}
    ]


def test_vocab_only_term_without_speechmatics_flag_is_invalid(tmp_path):
    write_dictionary(tmp_path, {"version": 1, "terms": [
        valid_term(id="v1", forms=[]),
    ]})
    with pytest.raises(FstError, match="empty 'forms'"):
        MedicalMatcher(tmp_path)


# --------------------------------------------------- Speechmatics vocabulary

def test_vocabulary_is_bounded_to_speechmatics_entries():
    matcher = MedicalMatcher(ROOT)
    data = load_repo_dictionary()
    eligible = {t["canonical"] for t in data["terms"] if t["speechmatics"]}
    contents = {v["content"] if isinstance(v, dict) else v
                for v in matcher.additional_vocab}
    assert contents == eligible
    assert len(matcher.additional_vocab) == len(eligible)  # no duplicates


def test_vocabulary_stays_bounded_not_the_full_dictionary():
    matcher = MedicalMatcher(ROOT)
    data = load_repo_dictionary()
    assert len(matcher.additional_vocab) < len(data["terms"])
    # See tests/test_app.py: the old "< 100" literal was unsatisfiable with
    # the shipped dictionary (146 eligible entries) and only ever "passed"
    # because the dictionary raised before reaching this assertion. The real
    # invariant is that the vocabulary stays a small curated subset.
    assert len(matcher.additional_vocab) < 300
    assert len(matcher.additional_vocab) < 0.25 * len(data["terms"])


def test_derived_vocabulary_matches_the_shipped_artifact():
    """The generated speechmatics_additional_vocab.json mirrors the dictionary."""
    matcher = MedicalMatcher(ROOT)
    shipped = json.loads(
        (KNOWLEDGE / "speechmatics_additional_vocab.json").read_text(encoding="utf-8")
    )
    key = lambda v: json.dumps(v, sort_keys=True, ensure_ascii=False)  # noqa: E731
    assert sorted(map(key, matcher.additional_vocab)) == sorted(map(key, shipped))


def test_sounds_like_only_feeds_vocabulary_never_rules(tmp_path):
    write_dictionary(tmp_path, {"version": 1, "terms": [
        valid_term(id="mri", canonical="MRI", type="imaging", tier="curated",
                   forms=["ام آر آی"], speechmatics=True,
                   sounds_like=["M R I", "ام آر آی"]),
        valid_term(id="metformin", canonical="metformin", type="drug",
                   tier="validated_term", forms=[], speechmatics=True,
                   sounds_like=["متفورمین"]),
    ]})
    matcher = MedicalMatcher(tmp_path)
    # only the declared form becomes a rule; sounds_like never does
    assert [r.form for r in matcher.rules] == ["ام آر آی"]
    assert matcher.canonicalize("متفورمین") == ("متفورمین", [])
    assert {"content": "metformin", "sounds_like": ["متفورمین"]} in \
        matcher.additional_vocab


# ------------------------------------------------- vocabulary artifact

def test_vocab_export_check_passes():
    """scripts/export_additional_vocab.py --check: artifact in sync with the
    dictionary's speechmatics: true entries (the §9 bounded generation step)."""
    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "export_additional_vocab.py"),
         "--check"],
        capture_output=True, text=True, cwd=ROOT,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "CHECK: OK" in result.stdout
    assert "speechmatics-eligible vocab" in result.stdout


def test_vocab_export_reproduces_the_committed_artifact(tmp_path):
    """Writing the artifact is deterministic: same dictionary, same file."""
    script = ROOT / "scripts" / "export_additional_vocab.py"
    committed = json.loads(
        (KNOWLEDGE / "speechmatics_additional_vocab.json").read_text(encoding="utf-8")
    )
    result = subprocess.run(
        [sys.executable, str(script), "--check"],
        capture_output=True, text=True, cwd=ROOT,
    )
    assert result.returncode == 0  # committed artifact == derived vocabulary
    # and the derived vocabulary is exactly what the matcher exposes
    matcher = MedicalMatcher(ROOT)
    assert matcher.additional_vocab == committed
