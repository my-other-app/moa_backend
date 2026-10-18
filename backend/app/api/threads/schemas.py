from typing import Literal, Optional

from pydantic import BaseModel, Field, field_validator

MAX_BODY = 500
MAX_OPTIONS = 4
MAX_OPTION = 60


class PollOptionOut(BaseModel):
    id: str
    label: str
    votes: int
    share: int


class PollOut(BaseModel):
    options: list[PollOptionOut]
    votes: int
    voted_option_id: Optional[str]


class ClubRef(BaseModel):
    id: int
    name: str
    slug: Optional[str] = None
    logo: Optional[dict] = None


class ThreadOut(BaseModel):
    id: str
    club: ClubRef
    body: str
    image: Optional[dict]
    kind: Literal["post", "poll"]
    poll: Optional[PollOut]
    pinned: bool
    notify_followers: bool
    likes: int
    comments: int
    views: int
    liked: bool
    created_at: str


class ThreadPage(BaseModel):
    items: list[ThreadOut]
    total: int
    limit: int
    offset: int
    # True when `total` stopped counting at its cap (the public feed counts at
    # most 1,000): there are more than `total`, and the pages go on.
    total_capped: bool = False


class Author(BaseModel):
    id: Optional[int]
    name: str
    is_club: bool


class CommentOut(BaseModel):
    id: str
    body: str
    author: Author
    created_at: str


class ThreadPatch(BaseModel):
    pinned: Optional[bool] = None
    body: Optional[str] = Field(None, min_length=1, max_length=MAX_BODY)


class CommentCreate(BaseModel):
    body: str = Field(..., min_length=1, max_length=500)

    @field_validator("body")
    @classmethod
    def _trim(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("Write something first")
        return v


class VoteIn(BaseModel):
    option_id: str


class LikeOut(BaseModel):
    liked: bool
    likes: int


class ThreadRowOut(ThreadOut):
    last_comment_at: Optional[str] = None
    awaiting_reply: bool = False
    engagement: int = 0


class ThreadsPageOut(BaseModel):
    items: list[ThreadRowOut]
    total: int
    limit: int
    offset: int
    counts: dict


class ThreadInsights(BaseModel):
    posts: int
    posts_delta: Optional[int] = None
    reach: int
    reach_delta: Optional[int] = None
    reach_per_follower: Optional[float] = None
    engagement_rate: Optional[float] = None
    engagement_rate_prev: Optional[float] = None
    awaiting: int
    best: Optional[dict] = None
    weeks: list[dict]
    kinds: list[dict]
    notify: dict
    followers: int
    all_time: dict
    generated_at: str
