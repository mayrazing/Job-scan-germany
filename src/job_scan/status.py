from job_scan.domain import (
    AvailabilityStatus,
    JobRecord,
    MachineStatus,
    PrimaryView,
    UserStatus,
)


def effective_status(job: JobRecord) -> MachineStatus:
    if job.machine_status is MachineStatus.EXCLUDED and job.manual_override == "show":
        return MachineStatus.ELIGIBLE
    return job.machine_status


def primary_view(job: JobRecord) -> PrimaryView | None:
    if job.user_status is UserStatus.APPLIED:
        return PrimaryView.APPLIED
    if job.availability_status is not AvailabilityStatus.ACTIVE:
        return None
    if job.user_status is UserStatus.IGNORED:
        return PrimaryView.IGNORED
    if job.user_status is UserStatus.REJECTED:
        return PrimaryView.REJECTED

    status = effective_status(job)
    if status is MachineStatus.EXCLUDED:
        return PrimaryView.EXCLUDED
    if status in {
        MachineStatus.PENDING_SOURCE,
        MachineStatus.PENDING,
        MachineStatus.UNCERTAIN,
    }:
        return PrimaryView.PENDING
    if job.user_status is UserStatus.SHORTLISTED:
        return PrimaryView.SHORTLISTED
    return PrimaryView.RECOMMENDED
