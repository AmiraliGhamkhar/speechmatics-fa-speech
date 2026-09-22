"""One-shot, audited dictionary repair applied during the v4 hardening pass.

Kept in the repository as the AUDIT TRAIL for every dictionary change: each
edit is listed with the reason it was made and the benchmark case that
caught it. Re-running the script is idempotent - it only removes entries
that are still present.

Run:  python scripts/_dictionary_fixes.py [--check]
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

DICTIONARY = ROOT / "medical_knowledge" / "medical_dictionary.json"


#: Aliases removed because the matcher is a *canonicalization* layer, not a
#: translator. Each of these is an ordinary, high-frequency Persian word;
#: mapping it to an English term rewrote plain clinical prose.
#:   {term_id: (forms_to_drop, reason)}
UNSAFE_ALIASES: dict[str, tuple[tuple[str, ...], str]] = {
    "heart": (("قلب",),
              "'قلب' is the ordinary noun: 'بخش قلب' became 'بخش heart' "
              "(benchmark para_admission / para_consult)"),
    "liver": (("کبد",), "ordinary anatomy noun; translation, not canonicalization"),
    "kidney": (("کلیه",), "ordinary anatomy noun; also collides with 'کلیه' = 'all'"),
    "stomach": (("معده",), "ordinary anatomy noun; translation, not canonicalization"),
    "brain": (("مغز",), "ordinary anatomy noun; translation, not canonicalization"),
    "alert-oriented": (("هوشیار",),
                       "'بیمار هوشیار' became 'بیمار alert and oriented' "
                       "(benchmark gram_echoed_verb); the full phrase "
                       "'هوشیار و اوریانته' is kept"),
    "history_0195": (("علائم",),
                     "'علائم' alone is 'signs/symptoms' in ordinary prose and "
                     "broke 'علائم پایدار است' (benchmark gram_repeated_punct)"),
    "misc_0110": (("صبح", "morning", "am", "a.m.", "AM"),
                  "'صبح' is the ordinary word 'morning'; rewriting it to the "
                  "English term corrupted 'ده و نیم صبح' (benchmark time_morning)"),
    "misc_0111": (("شب", "عصر", "pm", "p.m.", "PM", "evening"),
                  "'شب' is the ordinary word 'night' (benchmark time_night)"),
    "am": (("صبح", "قبل از ظهر"),
           "same ordinary-word collision as misc_0110, via the AM abbreviation"),
    "pm": (("شب", "بعد از ظهر", "عصر"),
           "same ordinary-word collision as misc_0111, via the PM abbreviation"),
    "anatomy_0281": (("پوستی", "skin"),
                     "'پوستی' is an ordinary adjective: 'کشش پوستی' became "
                     "'Traction dermatologic' (benchmark ortho_traction)"),
    "units_0033": (("نیم",),
                   "'نیم' is the ordinary word 'half' and is required by the "
                   "time rule 'ده و نیم' (benchmark time_half); the unit "
                   "abbreviation 'ss' is kept"),
    "traction": (("کشش",),
                 "'کشش' alone is ordinary ('tension/pull'); the specific "
                 "'کشش پوستی' -> 'skin traction' entry replaces it"),
    "iv-fluid": (("سرم",),
                 "'سرم' is used for any infusion bag in ordinary speech and "
                 "broke a dose sentence (benchmark num_dose_ml)"),
    "bedside": (("کنار تخت",),
                "same ordinary phrase as the POC alias below: 'نرده های کنار "
                "تخت' became 'نرده های Bedside' (benchmark para_fall)"),
    "poc": (("کنار تخت",),
            "'کنار تخت' is literally 'beside the bed' and became 'POC': "
            "'نرده های کنار تخت' -> 'نرده های POC' (benchmark para_fall)"),
    "secured": (("فیکس",),
                "'فیکس' is a Persianized 'fixed' used for any fixation; "
                "'برقرار و فیکس' became '... و Secured' (benchmark para_iv_line)"),
    "laboratory": (("lab",), "'lab' is ordinary English; handled by the "
                             "ambiguous-short-form uppercase guard instead"),
    "doctor": (("پزشک", "دکتر", "پزشک معالج"),
               "'دکتر احمدی'/'پزشک معالج' became 'doctor ...' - a translation, "
               "not a canonicalization (benchmark para_admission, "
               "para_physician_notify)"),
    "spo2": (("اشباع اکسیژن", "اکسیژن ساتوریشن", "ساتوریشن"),
             "these spoken forms belong to the 'oxygen saturation' term "
             "(pre-migration fixture + benchmark persianized_saturation); "
             "SpO2 keeps its own spoken form 'اس پی او دو'"),
    "inpatient-ward": (("بخش بستری",),
                       "'بخش بستری شد' is ordinary prose for 'was admitted to "
                       "the ward' and became 'inpatient ward شد' "
                       "(benchmark gram_spacing)"),
    "department_0128": (("him",), "'him' is a pronoun (HIM = health information "
                                  "management); uppercase-guarded instead"),
    "diagnosis_0175": (("شکستگی",),
                       "'شکستگی' alone is the generic word 'fracture' but the "
                       "site-specific phrase must win: 'شکستگی استخوان ران' "
                       "-> 'femur fracture' (benchmark ortho_fracture)"),
    "history_0188": (("سابقه جراحی",),
                     "matched inside 'سابقه جراحی قبلی ندارد' and produced "
                     "'past surgical history قبلی ندارد' (benchmark para_allergy)"),
    "fall-risk": (("خطر سقوط",),
                  "matched inside 'در معرض خطر سقوط می باشد' (benchmark "
                  "para_morse); the standalone scale terms are kept"),
    "department_0141": (("پذیرش", "admitting"),
                        "'پذیرش' is the ordinary word 'admission/reception' and "
                        "rewrote 'بیمار پذیرش شد' to '... admitting and "
                        "discharge شد' (benchmark gram_repeated_punct)"),
    "dyspnea": (("تنگی نفس",),
                "duplicate of misc_0108 'shortness of breath'; the spec lists "
                "'shortness of breath' as the required canonical"),
    "ekg": (("نوار قلب",),
            "duplicate spoken form of electrocardiogram; 'ECG' is the canonical "
            "the spec requires, so the Persian form routes there"),
}

#: Case-variant duplicate concepts. The dictionary carried BOTH
#: "vital signs"/"Vital signs" and "IV line"/"IV-Line" as separate terms, so
#: the same Persian form ("علائم حیاتی", "لاین وریدی") resolved to a
#: different canonical depending on tier order - exactly the "inconsistent
#: casing / conflicting canonicals" defect the audit asks for. Each pair is
#: merged into the surviving term; the loser's forms (including its own
#: spelling, so "IV-Line" still matches) move across.
#:   {loser_id: (winner_id, reason)}
MERGED_TERMS: dict[str, tuple[str, str]] = {
    "vital_signs": ("vital-signs",
                    "'Vital signs' duplicated 'vital signs' with different "
                    "casing and claimed the same Persian form"),
    "iv_line": ("iv-line",
                "'IV-Line' duplicated 'IV line' and claimed the same Persian "
                "form 'لاین وریدی'; the hyphenated spelling is kept as an alias"),
    "nurse_call": ("nurse-call-system",
                   "'nurse call' duplicated the spec's 'Nurse call' canonical"),
}

#: Canonical spellings corrected to the form the specification names.
#:   {term_id: (new_canonical, reason)}
RECASED_CANONICALS: dict[str, tuple[str, str]] = {
    "phlebitis": ("phlebitis",
                  "the spec lists 'phlebitis' in lowercase among the required "
                  "IV terminology canonicals"),
}

#: Numbers must not be medical dictionary terms (spec: "Do not model numbers
#: as normal medical dictionary terms"). The hour_1..hour_12 entries existed
#: only for time handling and are now covered by the deterministic
#: number/time stage in speechmatics_test/nursing_text.py.
REMOVED_TERM_IDS: dict[str, str] = {
    f"hour_{n}": "numeric-only term; time handled by nursing_text.py"
    for n in range(1, 13)
}

#: Terminology the spec asks for that the dictionary did not yet carry, plus
#: the site-specific phrases that must outrank the generic words removed above.
ADDED_TERMS: list[dict] = [
    {
        "id": "skin-turgor", "canonical": "skin turgor", "type": "phrase",
        "tier": "phrase", "forms": ["تورگور پوست", "تورگور پوستی", "اسکین تورگور"],
        "speechmatics": True, "sounds_like": ["تورگورپوست"],
        "source_file": "nursing_phrases.json",
    },
    {
        "id": "hydration-status", "canonical": "hydration status",
        "type": "phrase", "tier": "phrase",
        "forms": ["وضعیت هیدراتاسیون", "وضعیت آب بدن", "هیدریشن استاتوس"],
        "speechmatics": False, "sounds_like": [],
        "source_file": "nursing_phrases.json",
    },
    {
        "id": "initial-nursing-assessment",
        "canonical": "initial nursing assessment", "type": "phrase",
        "tier": "phrase", "forms": ["ارزیابی اولیه پرستاری", "بررسی اولیه پرستاری"],
        "speechmatics": False, "sounds_like": [],
        "source_file": "nursing_phrases.json",
    },
    {
        "id": "nurse-call-system", "canonical": "Nurse call", "type": "term",
        "tier": "validated_term",
        "forms": ["زنگ احضار پرستار", "زنگ پرستار", "احضار پرستار"],
        "speechmatics": True, "sounds_like": ["نرسکال"],
        "source_file": "nursing_terms.json",
    },
    {
        "id": "iv-infiltration", "canonical": "infiltration",
        "type": "condition", "tier": "validated_term",
        "forms": ["نشت دارویی", "نشت سرم", "انفیلتراسیون"],
        "speechmatics": False, "sounds_like": [],
        "source_file": "nursing_terms.json",
    },
    {
        "id": "iv-redness", "canonical": "redness", "type": "condition",
        "tier": "validated_term", "forms": ["قرمزی", "قرمزی موضعی", "ردنس"],
        "speechmatics": False, "sounds_like": [],
        "source_file": "nursing_terms.json",
    },
    {
        "id": "fall-precautions-fa", "canonical": "fall precautions",
        "type": "phrase", "tier": "phrase",
        "forms": ["احتیاطات سقوط", "اقدامات پیشگیری از سقوط",
                  "پیشگیری از سقوط", "فال پرکاشن"],
        "speechmatics": False, "sounds_like": [],
        "source_file": "nursing_phrases.json",
    },
    {
        "id": "femur-fracture", "canonical": "femur fracture",
        "type": "condition", "tier": "validated_term",
        "forms": ["شکستگی استخوان ران", "شکستگی فمور", "فرکچر فمور"],
        "speechmatics": False, "sounds_like": [],
        "source_file": "nursing_orthopedic_terms.json",
    },
    {
        "id": "skin-traction", "canonical": "skin traction", "type": "procedure",
        "tier": "validated_term",
        "forms": ["کشش پوستی", "تراکشن پوستی", "اسکین تراکشن"],
        "speechmatics": False, "sounds_like": [],
        "source_file": "nursing_orthopedic_terms.json",
    },
    {
        "id": "french-size", "canonical": "Fr", "type": "dosage_unit",
        "tier": "unit", "forms": ["فرنچ", "سایز فرنچ"],
        "speechmatics": False, "sounds_like": [],
        "source_file": "fst_terms.json",
    },
    {
        "id": "ptt-lab", "canonical": "PTT", "type": "lab",
        "tier": "abbreviation", "forms": ["پی تی تی"],
        "speechmatics": True, "sounds_like": ["پیتیتی"],
        "source_file": "abbreviations.json",
    },
]

#: Forms ADDED to existing terms (the canonical itself is unchanged).
ADDED_FORMS: dict[str, tuple[tuple[str, ...], str]] = {
    "misc_0108": (("تنگی نفس", "دیسپنه"),
                  "the spec requires 'shortness of breath' as the canonical "
                  "for this Persian phrase"),
    "electrocardiogram": (("نوار قلب", "ای سی جی"),
                          "route the Persian spoken form to the ECG canonical"),
    "ecg": (("نوار قلب",), "the spec lists ECG among the required canonicals"),
    "bedside_rails": (("نرده های کنار تخت", "نرده کنار تخت", "نرده\u200cهای کنار تخت"),
                      "replaces the removed generic 'کنار تخت' -> POC alias"),
    "nurse_call": (("نرس کال", "نرس\u200cکال"),
                   "keep the Persianized pronunciation on the canonical term"),
    "fall-precautions": (("احتیاطات سقوط", "احتیاط سقوط"),
                         "the spoken Persian form used in real nursing notes "
                         "was missing (benchmark fall_precautions, para_fall)"),
    "oxygen-saturation": (("اکسیژن ساتوریشن",),
                          "spoken form moved here from the SpO2 term"),
}


def apply_fixes(check: bool = False) -> int:
    data = json.loads(DICTIONARY.read_text(encoding="utf-8"))
    terms = data["terms"]
    by_id = {t["id"]: t for t in terms}
    changes: list[str] = []

    # 1. drop unsafe aliases
    for term_id, (forms, reason) in UNSAFE_ALIASES.items():
        term = by_id.get(term_id)
        if term is None:
            continue
        dropped = [f for f in term["forms"] if f in forms]
        if dropped:
            term["forms"] = [f for f in term["forms"] if f not in forms]
            term["sounds_like"] = [
                s for s in term.get("sounds_like", []) if s not in forms
            ]
            changes.append(
                f"{term_id}: dropped aliases {dropped} - {reason}")

    # 2. remove numeric-only terms
    for term_id, reason in REMOVED_TERM_IDS.items():
        if term_id in by_id:
            terms.remove(by_id[term_id])
            changes.append(f"{term_id}: removed term - {reason}")

    # 3. add missing forms to existing terms
    for term_id, (forms, reason) in ADDED_FORMS.items():
        term = by_id.get(term_id)
        if term is None:
            continue
        added = [f for f in forms if f not in term["forms"]]
        if added:
            term["forms"].extend(added)
            changes.append(f"{term_id}: added forms {added} - {reason}")

    # 3b. merge case-variant duplicate concepts
    for loser_id, (winner_id, reason) in MERGED_TERMS.items():
        loser, winner = by_id.get(loser_id), by_id.get(winner_id)
        if loser is None:
            continue
        if winner is None:
            # The winner is one of the ADDED_TERMS: rename in place instead.
            continue
        moved = [loser["canonical"]] + list(loser["forms"])
        winner["forms"].extend(
            f for f in moved if f not in winner["forms"]
        )
        winner["sounds_like"] = list(winner.get("sounds_like", [])) + [
            s for s in loser.get("sounds_like", [])
            if s not in winner.get("sounds_like", [])
        ]
        winner["speechmatics"] = bool(
            winner.get("speechmatics") or loser.get("speechmatics"))
        terms.remove(loser)
        changes.append(
            f"{loser_id}: merged into {winner_id} - {reason}")

    # 3c. canonical spelling corrections
    for term_id, (canonical, reason) in RECASED_CANONICALS.items():
        term = by_id.get(term_id)
        if term is None or term["canonical"] == canonical:
            continue
        old = term["canonical"]
        if old not in term["forms"]:
            term["forms"].append(old)  # keep the old spelling matchable
        term["canonical"] = canonical
        changes.append(
            f"{term_id}: canonical {old!r} -> {canonical!r} - {reason}")

    # 4. add new terms
    existing_canonicals = {t["canonical"] for t in terms}
    for new_term in ADDED_TERMS:
        if new_term["id"] in by_id:
            continue
        if new_term["canonical"] in existing_canonicals:
            continue
        terms.append(dict(new_term))
        existing_canonicals.add(new_term["canonical"])
        changes.append(f"{new_term['id']}: added term "
                       f"{new_term['canonical']!r}")

    # 5. drop terms left with no forms and no vocabulary role
    orphans = [
        t for t in terms
        if not t["forms"] and not t.get("speechmatics")
    ]
    for term in orphans:
        terms.remove(term)
        changes.append(f"{term['id']}: removed - no forms left and not a "
                       f"vocabulary entry")

    # 6. metadata must match reality
    data["metadata"]["entry_count"] = len(terms)
    data["metadata"]["generated_on"] = "2026-09-22"
    data["metadata"]["note"] = (
        "Deduplicated by id and canonical. Numeric-only hour terms removed "
        "(numbers/time are handled deterministically in "
        "speechmatics_test/nursing_text.py). Ordinary-word aliases that "
        "translated plain Persian/English prose were dropped; see "
        "scripts/_dictionary_fixes.py for the per-entry audit trail."
    )

    if check:
        for change in changes:
            print("WOULD APPLY:", change)
        print(f"\n{len(changes)} pending change(s); terms would be {len(terms)}")
        return 1 if changes else 0

    DICTIONARY.write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    for change in changes:
        print("applied:", change)
    print(f"\n{len(changes)} change(s) applied; dictionary now has "
          f"{len(terms)} terms")
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    raise SystemExit(apply_fixes(args.check))
