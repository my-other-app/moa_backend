from fastapi import APIRouter, Request
from fastapi.responses import FileResponse

from app.api.audit import service as audit
from app.core.auth.permissions import ExportDataAuth
from app.core.rate_limit import limiter
from app.core.response.base_model import CustomBaseModel
from app.db.core import SessionDep
from app.api.backups import offsite, service

router = APIRouter(prefix="/admin/backups", tags=["backups"])


class BackupOut(CustomBaseModel):
    name: str
    size_bytes: int
    created_at: str
    # Where the off-site copy went (s3 key), or why it did not; both None
    # when off-site copies are off (APP_BACKUP_S3_BUCKET unset).
    offsite_key: str | None = None
    offsite_error: str | None = None


class BackupListOut(CustomBaseModel):
    items: list[BackupOut]
    directory: str
    # So the dashboard can say "this is on the same disk as the database"
    # without hard-coding an assumption about where that is.
    is_offsite: bool = False


def _out(backup: service.Backup, copy: service.OffsiteResult | None = None) -> BackupOut:
    return BackupOut(
        name=backup.name,
        size_bytes=backup.size_bytes,
        created_at=backup.created_at.isoformat(),
        offsite_key=copy.key if copy else None,
        offsite_error=copy.error if copy else None,
    )


@router.get("", summary="List database snapshots (admin)")
async def list_backups(admin: ExportDataAuth) -> BackupListOut:
    return BackupListOut(
        items=[_out(b) for b in service.list_backups()],
        directory=str(service.backup_dir()),
        is_offsite=offsite.enabled(),
    )


@router.post("", summary="Take a database snapshot (admin)")
# Dumping is IO-heavy on a small instance, and nothing is gained by running
# several at once.
@limiter.limit("3/hour")
async def create_backup(
    request: Request, session: SessionDep, admin: ExportDataAuth
) -> BackupOut:
    backup = await service.create_backup()
    copy = await service.copy_offsite(backup)
    await audit.record(
        session,
        admin,
        action="backup.create",
        target=backup.name,
        request=request,
    )
    await session.commit()
    return _out(backup, copy)


@router.get("/{name}", summary="Download a database snapshot (admin)")
async def download_backup(
    name: str, request: Request, session: SessionDep, admin: ExportDataAuth
) -> FileResponse:
    """
    Hands over the entire database in one response, so it is logged before the
    file is sent rather than after -- a download that fails halfway still
    happened as far as anyone reviewing the log is concerned.
    """
    path = service.path_for_download(name)
    await audit.record(
        session, admin, action="backup.download", target=name, request=request
    )
    await session.commit()
    return FileResponse(
        path, media_type="application/octet-stream", filename=name
    )


@router.delete("/{name}", summary="Delete a database snapshot (admin)")
async def delete_backup(
    name: str, request: Request, session: SessionDep, admin: ExportDataAuth
) -> dict:
    service.delete_backup(name)
    await audit.record(
        session, admin, action="backup.delete", target=name, request=request
    )
    await session.commit()
    return {"deleted": name}
