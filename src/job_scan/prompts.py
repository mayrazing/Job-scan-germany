from __future__ import annotations

import json
from collections.abc import Sequence
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from job_scan.domain import JobRecord
    from job_scan.setup_service import SetupAnswers

PROFILE_HEADINGS = (
    "Target roles",
    "Technical skills",
    "Experience",
    "Languages",
    "Work authorization and visa",
    "Preferences",
)


def build_profile_prompt(resume_text: str, answers: SetupAnswers) -> str:
    """Build the private stdin prompt for one factual Markdown profile."""
    preferences: dict[str, Any] = {
        "search_terms": answers.search_terms,
        "locations": answers.locations,
        "german_level": answers.german_level,
    }
    headings = "\n".join(f"# {heading}" for heading in PROFILE_HEADINGS)
    return (
        "Create a concise Markdown job-search profile from the resume and stated "
        "preferences below. Do not invent, infer, embellish, or silently fill missing "
        "facts. State unknown facts as unknown. Return every required heading with "
        "non-empty content.\n\n"
        f"Required Markdown headings:\n{headings}\n\n"
        "Setup preferences (JSON):\n"
        f"{json.dumps(preferences, ensure_ascii=False, separators=(',', ':'))}\n\n"
        "Resume text:\n"
        f"{resume_text}"
    )


def build_review_prompt(
    jobs: Sequence[JobRecord],
    profile: str,
) -> str:
    """Build one private semantic-review prompt with complete plain-text JDs."""
    submitted_jobs = [
        {
            "job_key": item.canonical_job_key,
            "title": item.title,
            "company": item.company,
            "location": item.location,
            "source_company_industry": (
                item.company_industry.industry
                if item.company_industry is not None
                else None
            ),
            "complete_jd": item.description,
        }
        for item in sorted(jobs, key=lambda item: item.canonical_job_key)
    ]
    jobs_json = json.dumps(
        submitted_jobs,
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return (
        "System instruction: Review each submitted job against the candidate profile. "
        "Report semantic facts and a score only; Python applies exclusion policy. "
        "The score is the technical and professional skill match between the complete "
        "job description and the candidate's demonstrated professional experience. "
        "Compare the core job responsibilities, required skills, preferred skills, "
        "production technologies, seniority, and depth of hands-on experience. Give "
        "the most weight to core responsibilities and required skills. Treat preferred "
        "skills as secondary. Do not infer experience from a job title or from a skill "
        "that is merely named without evidence of professional use. Use this stable "
        "rubric: 90-100 means the core work and nearly all required skills are directly "
        "demonstrated at comparable depth; 75-89 means a strong core match with only "
        "minor gaps; 60-74 means a workable or transferable match with one important "
        "skill or domain gap; 40-59 means partial overlap but missing core work or "
        "several important skills; 0-39 means little relevant professional experience. "
        "The reason must name the strongest matching skills and important missing "
        "skills that caused the score. German, visa sponsorship, existing work "
        "authorization, citizenship, security clearance, and staffing-agency status "
        "must not affect the technical skill score; report them only in their dedicated "
        "fields. "
        "Treat an explicit hard German requirement as german_requirement=required. "
        "Treat optional or preferred German as german_requirement=optional. "
        "A German-language job description alone is document language, not evidence "
        "that German is required. Optional or preferred German, and the fact that a "
        "job description is written in German, must not lower the score. German "
        "requirements are informational only and are not exclusion facts. If visa "
        "sponsorship is unmentioned, return "
        "visa_sponsorship=not_mentioned. For exclusion facts, return verbatim "
        "eligibility evidence copied only from that job's complete plain-text JD. "
        "An exclusion fact is visa not offered, existing German work authorization "
        "required, or German/EU citizenship required. Copy every evidence "
        "string byte-for-byte as an exact contiguous substring; do not paraphrase, "
        "change case, normalize Unicode, or collapse whitespace. Return job_key as the "
        "submitted canonical key. Also report the company's industry, which is distinct "
        "from the job function, department, business unit, or required skills. If "
        "source_company_industry is present, copy it exactly, use high confidence, and "
        "return no company_industry_evidence. Otherwise infer company industry only from "
        "complete_jd text that describes the company's products, services, or market. "
        "Do not infer it from the job title, job function, department, business unit, or "
        "skills. Copy at least one supporting company_industry_evidence string as an exact "
        "contiguous substring of complete_jd. If complete_jd lacks enough company-level "
        "evidence, return company_industry=null, company_industry_confidence=low, and an "
        "empty company_industry_evidence list.\n\n"
        "Candidate profile:\n"
        f"{profile}\n\n"
        "Submitted jobs (JSON; complete_jd is the complete plain-text JD):\n"
        f"{jobs_json}"
    )


def build_ats_resume_prompt(resume_text: str) -> str:
    """Build one text-only ATS readiness prompt."""
    return (
        "Evaluate ATS content readiness from extracted resume text only. The parser "
        "already confirmed selectable text. Check whether contact details, experience, "
        "education, skills, dates, titles, employers, and measurable outcomes are clear. "
        "Do not claim to detect columns, tables, fonts, graphics, or visual layout because "
        "no rendered document was provided. Keep every finding grounded in the supplied "
        "text and do not invent missing facts. Return the requested structured output.\n\n"
        "Resume text:\n"
        f"{resume_text}"
    )


def build_ats_job_prompt(resume_text: str, job: JobRecord) -> str:
    """Build one resume-to-one-JD ATS fit prompt."""
    submitted_job = json.dumps(
        {
            "job_key": job.canonical_job_key,
            "title": job.title,
            "company": job.company,
            "location": job.location,
            "complete_jd": job.description,
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return (
        "Compare one resume with exactly one complete job description. Score required "
        "skills, demonstrated experience and seniority, and supported JD keywords. "
        "Matched claims and suggestions must be grounded in the resume. Never recommend "
        "adding a skill or fact unless the resume already supports it. Return job_key "
        "exactly as supplied. This is a fit estimate, not a screening guarantee.\n\n"
        "Resume text:\n"
        f"{resume_text}\n\n"
        "Submitted job (JSON):\n"
        f"{submitted_job}"
    )
