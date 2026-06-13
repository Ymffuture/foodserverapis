# models/social.py
"""
Generic social interaction models.
item_type + item_id target any content type (menu_item, review, etc.)
"""
from beanie import Document
from pydantic import Field
from datetime import datetime
from typing import Optional
from enum import Enum


class ItemType(str, Enum):
    MENU_ITEM = "menu_item"
    REVIEW    = "review"


class Comment(Document):
    item_id:       str
    item_type:     ItemType

    # Author (cached — no join needed on read)
    user_id:       str
    user_name:     str
    user_email:    str
    user_picture:  Optional[str] = None

    content:       str  = Field(..., min_length=1, max_length=1000)
    parent_id:     Optional[str] = None    # None = top-level; str = reply to comment
    like_count:    int  = 0                # denormalised for fast reads
    is_visible:    bool = True             # admin soft-delete

    created_at:    datetime = Field(default_factory=datetime.utcnow)
    updated_at:    datetime = Field(default_factory=datetime.utcnow)

    class Settings:
        name = "comments"
        indexes = [
            [("item_type", 1), ("item_id", 1), ("parent_id", 1), ("created_at", 1)],
            "user_id",
            "is_visible",
        ]


class CommentLike(Document):
    """One record per (user, comment) pair."""
    comment_id: str
    user_id:    str
    created_at: datetime = Field(default_factory=datetime.utcnow)

    class Settings:
        name = "comment_likes"
        indexes = [
            [("comment_id", 1), ("user_id", 1)],
            "comment_id",
        ]


class Like(Document):
    """One record per (user, item) pair."""
    item_type:  ItemType
    item_id:    str
    user_id:    str
    created_at: datetime = Field(default_factory=datetime.utcnow)

    class Settings:
        name = "social_likes"
        indexes = [
            [("item_type", 1), ("item_id", 1), ("user_id", 1)],
            [("item_type", 1), ("item_id", 1)],
        ]


class Bookmark(Document):
    """One record per (user, item) pair."""
    item_type:  ItemType
    item_id:    str
    user_id:    str
    created_at: datetime = Field(default_factory=datetime.utcnow)

    class Settings:
        name = "bookmarks"
        indexes = [
            [("item_type", 1), ("item_id", 1), ("user_id", 1)],
            [("user_id",   1), ("item_type", 1)],
        ]


class Share(Document):
    """Tracks every share event — used for analytics."""
    item_type:  ItemType
    item_id:    str
    user_id:    str
    platform:   str  # copy | twitter | facebook | native
    created_at: datetime = Field(default_factory=datetime.utcnow)

    class Settings:
        name = "shares"
        indexes = [
            [("item_type", 1), ("item_id", 1)],
            "user_id",
            "platform",
        ]
