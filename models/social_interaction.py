# backend/models/social_interaction.py

from datetime import datetime
from typing import Optional, List
from pydantic import BaseModel, Field
from beanie import Document
from bson import ObjectId


# ─────────────────────────────
# Request / DTO Schemas
# ─────────────────────────────

class LikeToggle(BaseModel):
    item_id: str
    item_type: str


class CommentCreate(BaseModel):
    item_id: str
    item_type: str
    content: str
    parent_comment_id: Optional[str] = None


class CommentEdit(BaseModel):
    content: str


class ShareRecordCreate(BaseModel):
    item_id: str
    item_type: str
    platform: str


class BookmarkToggle(BaseModel):
    item_id: str
    item_type: str


# ─────────────────────────────
# Embedded Models
# ─────────────────────────────

class CommentReply(BaseModel):
    id: str = Field(default_factory=lambda: str(ObjectId()))
    user_id: str
    user_name: str
    user_avatar_url: Optional[str] = None
    content: str
    likes: int = 0
    liked_by: List[str] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=datetime.utcnow)
    is_edited: bool = False
    edited_at: Optional[datetime] = None


class Comment(BaseModel):
    id: str = Field(default_factory=lambda: str(ObjectId()))
    user_id: str
    user_name: str
    user_avatar_url: Optional[str] = None
    content: str
    likes: int = 0
    liked_by: List[str] = Field(default_factory=list)
    replies: List[CommentReply] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=datetime.utcnow)
    is_edited: bool = False
    edited_at: Optional[datetime] = None
    is_deleted: bool = False
    deleted_at: Optional[datetime] = None


class ShareRecord(BaseModel):
    user_id: Optional[str] = None
    platform: str
    created_at: datetime = Field(default_factory=datetime.utcnow)


# ─────────────────────────────
# MAIN DOCUMENT
# ─────────────────────────────

class SocialInteraction(Document):
    item_id: str
    item_type: str

    likes: int = 0
    liked_by: List[str] = Field(default_factory=list)

    comments: List[Comment] = Field(default_factory=list)
    comment_count: int = 0

    shares: List[ShareRecord] = Field(default_factory=list)
    share_count: int = 0

    bookmarks: List[str] = Field(default_factory=list)
    bookmark_count: int = 0

    total_engagement_score: int = 0

    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)

    class Settings:
        name = "social_interactions"

    # ───────────── private helpers ─────────────

    async def _recalc(self):
        active_comments = len([c for c in self.comments if not c.is_deleted])
        active_replies = sum(len(c.replies) for c in self.comments)

        self.total_engagement_score = (
            self.likes
            + active_comments * 3
            + active_replies * 2
            + self.share_count * 5
        )
        self.updated_at = datetime.utcnow()

    # ───────────── class methods ─────────────

    @classmethod
    async def get_or_create(cls, item_id: str, item_type: str) -> "SocialInteraction":
        obj = await cls.find_one({"item_id": item_id, "item_type": item_type})
        if not obj:
            obj = cls(item_id=item_id, item_type=item_type)
            await obj.insert()
        return obj

    # ───────────── likes ─────────────

    async def toggle_like(self, user_id: str) -> dict:
        if user_id in self.liked_by:
            self.liked_by.remove(user_id)
            self.likes = max(0, self.likes - 1)
            liked = False
        else:
            self.liked_by.append(user_id)
            self.likes += 1
            liked = True

        await self._recalc()
        await self.save()
        return {"liked": liked, "count": self.likes}

    # ───────────── comments ─────────────

    async def add_comment(
        self,
        user_id: str,
        user_name: str,
        content: str,
        user_avatar_url: Optional[str] = None,
    ) -> Comment:
        comment = Comment(
            user_id=user_id,
            user_name=user_name,
            content=content,
            user_avatar_url=user_avatar_url,
        )
        self.comments.append(comment)
        self.comment_count = len([c for c in self.comments if not c.is_deleted])

        await self._recalc()
        await self.save()
        return comment

    async def add_reply(
        self,
        parent_comment_id: str,
        user_id: str,
        user_name: str,
        content: str,
        user_avatar_url: Optional[str] = None,
    ) -> Optional[CommentReply]:
        for comment in self.comments:
            if comment.id == parent_comment_id and not comment.is_deleted:
                reply = CommentReply(
                    user_id=user_id,
                    user_name=user_name,
                    content=content,
                    user_avatar_url=user_avatar_url,
                )
                comment.replies.append(reply)
                self.comment_count = len([c for c in self.comments if not c.is_deleted])

                await self._recalc()
                await self.save()
                return reply

        return None  # parent not found

    async def delete_comment(self, comment_id: str, user_id: str) -> bool:
        """Soft-delete. Admins could skip the user_id check upstream."""
        for comment in self.comments:
            if comment.id == comment_id:
                if comment.user_id != user_id:
                    return False  # not the owner
                comment.is_deleted = True
                comment.deleted_at = datetime.utcnow()
                self.comment_count = len([c for c in self.comments if not c.is_deleted])

                await self._recalc()
                await self.save()
                return True

        return False  # comment not found

    async def like_comment(self, comment_id: str, user_id: str) -> bool:
        """Toggle like on a top-level comment. Returns new liked state."""
        for comment in self.comments:
            if comment.id == comment_id and not comment.is_deleted:
                if user_id in comment.liked_by:
                    comment.liked_by.remove(user_id)
                    comment.likes = max(0, comment.likes - 1)
                else:
                    comment.liked_by.append(user_id)
                    comment.likes += 1

                await self.save()
                return user_id in comment.liked_by

        return False

    # ───────────── shares ─────────────

    async def record_share(
        self, platform: str, user_id: Optional[str] = None
    ) -> dict:
        record = ShareRecord(platform=platform, user_id=user_id)
        self.shares.append(record)
        self.share_count += 1

        await self._recalc()
        await self.save()
        return {"total_shares": self.share_count}

    # ───────────── bookmarks ─────────────

    async def toggle_bookmark(self, user_id: str) -> dict:
        if user_id in self.bookmarks:
            self.bookmarks.remove(user_id)
            self.bookmark_count = max(0, self.bookmark_count - 1)
            bookmarked = False
        else:
            self.bookmarks.append(user_id)
            self.bookmark_count += 1
            bookmarked = True

        self.updated_at = datetime.utcnow()
        await self.save()
        return {"bookmarked": bookmarked, "count": self.bookmark_count}
