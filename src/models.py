"""Strong, typed canonical model (Pydantic v2).

This is the parser's validation core: it coerces types, enforces ranges,
forbids unknown fields, and emits precise per-field errors. It also serves as
the single source of truth for student.schema.json (see scripts/gen_schema.py).
No LLM is involved here.
"""
from __future__ import annotations

from typing import List, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, ValidationError  # noqa: F401

_Strict = ConfigDict(extra="forbid")          # reject unknown fields
GPA_SCALES = (4.0, 5.0, 10.0, 20.0, 100.0)


class GPA(BaseModel):
    model_config = _Strict
    raw: float = Field(ge=0)
    scale: float
    normalized_4: float = Field(ge=0, le=4.0)

    def model_post_init(self, _):  # validate scale membership clearly
        if self.scale not in GPA_SCALES:
            raise ValueError(f"gpa.scale must be one of {GPA_SCALES}, got {self.scale}")


class EducationEntry(BaseModel):
    model_config = _Strict
    index: int = Field(ge=0)
    college_name: Optional[str] = None
    country: Optional[str] = None
    state: Optional[str] = None
    degree: Optional[str] = None
    major: Optional[str] = None
    gpa: Optional[GPA] = None


class IELTSAttempt(BaseModel):
    model_config = _Strict
    attempt: int = Field(ge=0)
    overall_band: float = Field(ge=0, le=9)


class TOEFLAttempt(BaseModel):
    model_config = _Strict
    attempt: int = Field(ge=0)
    listening: Optional[float] = Field(default=None, ge=0, le=30)
    reading: Optional[float] = Field(default=None, ge=0, le=30)
    speaking: Optional[float] = Field(default=None, ge=0, le=30)
    writing: Optional[float] = Field(default=None, ge=0, le=30)
    total: Optional[float] = Field(default=None, ge=0, le=120)


class EnglishProficiency(BaseModel):
    model_config = _Strict
    ielts: Optional[List[IELTSAttempt]] = None
    toefl: Optional[List[TOEFLAttempt]] = None
    best_ielts_overall: Optional[float] = Field(default=None, ge=0, le=9)
    best_toefl_total: Optional[float] = Field(default=None, ge=0, le=120)
    best_duolingo: Optional[float] = Field(default=None, ge=0, le=160)
    best_pte: Optional[float] = Field(default=None, ge=0, le=90)
    # TAMU's own English Language Proficiency Exam: reported as written
    # ("Pass", "Exempt", a score), so it stays text.
    elpe: Optional[str] = None


class GRESections(BaseModel):
    """GRE section scores as the export reports them. The 130-170 scale is
    current; older exports carry the 200-800 scale, so the cap is the old one."""
    model_config = _Strict
    verbal: Optional[float] = Field(default=None, ge=0, le=800)
    quant: Optional[float] = Field(default=None, ge=0, le=800)
    writing: Optional[float] = Field(default=None, ge=0, le=6)
    official: Optional[bool] = None


class GPASummary(BaseModel):
    """CAS-calculated cumulative GPAs (4.0 scale). CAS writes 0 when it didn't
    calculate one (non-US transcripts), so 0 is dropped as missing upstream."""
    model_config = _Strict
    undergraduate: Optional[float] = Field(default=None, ge=0, le=5)
    overall: Optional[float] = Field(default=None, ge=0, le=5)
    graduate: Optional[float] = Field(default=None, ge=0, le=5)


class ExperienceEntry(BaseModel):
    model_config = _Strict
    index: int = Field(ge=0)
    designation: Optional[str] = None
    local_status: Optional[str] = None


class ProgramEntry(BaseModel):
    """A program the applicant applied to (from the designation_* columns)."""
    model_config = _Strict
    index: int = Field(ge=0)
    name: Optional[str] = None
    label: Optional[str] = None
    status: Optional[str] = None
    department: Optional[str] = None
    level: Optional[str] = None
    start_term: Optional[str] = None
    start_year: Optional[str] = None


class Interests(BaseModel):
    model_config = _Strict
    areas: Optional[List[str]] = None
    specialization: Optional[List[str]] = None


class Personal(BaseModel):
    model_config = _Strict
    first_name: Optional[str] = None
    last_name: Optional[str] = None
    full_name: Optional[str] = None
    email: Optional[str] = None


class Meta(BaseModel):
    model_config = _Strict
    source_file: str
    source_row: Optional[int] = Field(default=None, ge=0)
    ingested_at: str
    validation_status: Literal["valid", "valid_with_warnings"]
    warnings: Optional[List[str]] = None


class StudentRecord(BaseModel):
    """Strict, validated representation of a single applicant."""
    model_config = _Strict
    schema_version: str = Field(pattern=r"^\d+\.\d+\.\d+$")
    cas_id: str = Field(min_length=1)
    personal: Optional[Personal] = None
    english_proficiency: Optional[EnglishProficiency] = None
    gre_results: Optional[List[float]] = Field(
        default=None,
        description="Raw GRE result values as reported; semantics vary by source "
                    "so these are stored loosely and NOT used for scoring.")
    education: Optional[List[EducationEntry]] = None
    experience: Optional[List[ExperienceEntry]] = None
    programs: Optional[List[ProgramEntry]] = None
    interests: Optional[Interests] = None
    gre: Optional[GRESections] = None
    gpa_summary: Optional[GPASummary] = None
    # Only ever "Domestic" or "International", read off the program the
    # applicant chose ("... - International Students") or the export's status
    # column. The country itself is never stored.
    residency: Optional[Literal["Domestic", "International"]] = None
    awards: Optional[List[str]] = None
    # Field of study as the applicant typed it (custom_field_background).
    background: Optional[str] = None
    # Answers to any application question about courses (distance degree plans).
    course_answers: Optional[List[str]] = None
    meta: Meta


def parse_record(data: dict):
    """Strongly parse/validate an assembled dict.

    Returns (record_dict_or_None, [error_strings]). On success the dict is the
    cleaned, type-normalized model dump (None fields dropped).
    """
    try:
        model = StudentRecord.model_validate(data)
    except ValidationError as exc:
        errors = [f"{'/'.join(str(p) for p in e['loc']) or '<root>'}: {e['msg']}"
                  for e in exc.errors()]
        return None, errors
    return model.model_dump(mode="json", exclude_none=True), []
