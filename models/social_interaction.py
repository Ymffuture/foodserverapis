# backend/models/social_interaction.py
from datetime import datetime
from typing import Optional, List
from pydantic import BaseModel, Field, ConfigDict
from beanie import Document, EmbeddedModel
from bson import ObjectId


# ─────────────────────────────────────────────
# Embedded Models (FIXED for Beanie)
# ─────────────────────────────────────────────

class CommentReply(EmbeddedModel):
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


class Comment(EmbeddedModel):
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


class ShareRecord(EmbeddedModel):
    user_id: Optional[str] = None
    platform: str
    created_at: datetime = Field(default_factory=datetime.utcnow)


# ─────────────────────────────────────────────
# Main Document
# ─────────────────────────────────────────────

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
        indexes = [
            [("item_id", 1), ("item_type", 1)],
            [("total_engagement_score", -1)],
            [("updated_at", -1)],
        ]

    # ───── LIKE ─────
    async def toggle_like(self, user_id: str):
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

    # ───── COMMENT ─────
    async def add_comment(self, user_id, name, content, avatar=None):
        comment = Comment(
            user_id=user_id,
            user_name=name,
            user_avatar_url=avatar,
            content=content,
        )

        self.comments = [comment] + self.comments
        self.comment_count = len([c for c in self.comments if not c.is_deleted])

        await self._recalc()
        await self.save()

        return comment

    async def add_reply(self, comment_id, user_id, name, content, avatar=None):
        for c in self.comments:
            if c.id == comment_id and not c.is_deleted:
                reply = CommentReply(
                    user_id=user_id,
                    user_name=name,
                    user_avatar_url=avatar,
                    content=content,
                )

                c.replies = [reply] + c.replies
                await self._recalc()
                await self.save()
                return reply
        return None

    async def like_comment(self, comment_id, user_id):
        for c in self.comments:
            if c.id == comment_id:
                if user_id not in c.liked_by:
                    c.liked_by.append(user_id)
                    c.likes += 1
                    await self.save()
                return True
        return False

    async def delete_comment(self, comment_id, user_id, is_admin=False):
        for c in self.comments:
            if c.id == comment_id:
                if c.user_id == user_id or is_admin:
                    c.is_deleted = True
                    c.deleted_at = datetime.utcnow()
                    self.comment_count = max(0, self.comment_count - 1)
                    await self._recalc()
                    await self.save()
                    return True
        return False

    async def record_share(self, platform, user_id=None):
        self.shares.append(ShareRecord(user_id=user_id, platform=platform))
        self.share_count += 1

        await self._recalc()
        await self.save()

        return {"total_shares": self.share_count}

    async def toggle_bookmark(self, user_id: str):
        if user_id in self.bookmarks:
            self.bookmarks.remove(user_id)
            self.bookmark_count = max(0, self.bookmark_count - 1)
            state = False
        else:
            self.bookmarks.append(user_id)
            self.bookmark_count += 1
            state = True

        await self.save()
        return {"bookmarked": state, "count": self.bookmark_count}

    async def _recalc(self):
        active_comments = len([c for c in self.comments if not c.is_deleted])
        active_replies = sum(len(c.replies) for c in self.comments)

        self.total_engagement_score = (
            self.likes +
            active_comments * 3 +
            active_replies * 2 +
            self.share_count * 5
        )

        self.updated_at = datetime.utcnow()

    @classmethod
    async def get_or_create(cls, item_id, item_type):
        obj = await cls.find_one({"item_id": item_id, "item_type": item_type})
        if not obj:
            obj = cls(item_id=item_id, item_type=item_type)
            await obj.insert()
        return obj
