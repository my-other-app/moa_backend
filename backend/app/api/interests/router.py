from fastapi import APIRouter

from app.db.core import SessionDep

router = APIRouter(prefix="/interests")
