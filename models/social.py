# backend/models/social_interaction.py
from datetime import datetime
from typing import Optional, List
from pydantic import BaseModel, Field, ConfigDict
from beanie import Document, Indexed, before_event, Insert, Update
from bson import ObjectId


class CommentReply(BaseModel):
    """Nested reply to a comment."""
    id: str = Field(default_factory=lambda: str(ObjectId()))
    user_id: Indexed(str)
    user_name: str
    user_avatar_url: Optional[str] = None
    content: str = Field(..., min_length=1, max_length=1000)
    likes: int = 0
    liked_by: List[str] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=datetime.utcnow)
    is_edited: bool = False
    edited_at: Optional[datetime] = None
    
    model_config = ConfigDict(json_schema_extra={
        "example": {
            "user_id": "user_123",
            "user_name": "John Doe",
            "content": "Great point!",
            "likes": 5,
            "created_at": "2024-01-15T10:30:00Z"
        }
    })


class Comment(BaseModel):
    """Comment on a review/post."""
    id: str = Field(default_factory=lambda: str(ObjectId()))
    user_id: Indexed(str)
    user_name: str
    user_avatar_url: Optional[str] = None
    content: str = Field(..., min_length=1, max_length=2000)
    likes: int = 0
    liked_by: List[str] = Field(default_factory=list)
    replies: List[CommentReply] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=datetime.utcnow)
    is_edited: bool = False
    edited_at: Optional[datetime] = None
    is_deleted: bool = False
    deleted_at: Optional[datetime] = None
    
    model_config = ConfigDict(json_schema_extra={
        "example": {
            "user_id": "user_123",
            "user_name": "John Doe",
            "content": "This is amazing!",
            "likes": 12,
            "replies": [],
            "created_at": "2024-01-15T10:30:00Z"
        }
    })


class ShareRecord(BaseModel):
    """Record of a share action."""
    user_id: Optional[str] = None  # Anonymous shares allowed
    platform: str = Field(..., pattern="^(copy|twitter|facebook|whatsapp|email|native|other)$")
    created_at: datetime = Field(default_factory=datetime.utcnow)
    ip_hash: Optional[str] = None  # For deduplication without storing raw IP
    
    model_config = ConfigDict(json_schema_extra={
        "example": {
            "platform": "twitter",
            "created_at": "2024-01-15T10:30:00Z"
        }
    })


class SocialInteraction(Document):
    """
    Beanie Document for social interactions (likes, comments, shares)
    on reviews, posts, products, or any content item.
    """
    
    # Target item
    item_id: Indexed(str)  # ID of the item being interacted with
    item_type: str = Field(..., pattern="^(review|product|post|menu_item|order)$")
    
    # Engagement counters
    likes: int = Field(default=0, ge=0)
    liked_by: List[str] = Field(default_factory=list)  # User IDs who liked
    
    comments: List[Comment] = Field(default_factory=list)
    comment_count: int = Field(default=0, ge=0)  # Denormalized for fast queries
    
    shares: List[ShareRecord] = Field(default_factory=list)
    share_count: int = Field(default=0, ge=0)  # Denormalized
    
    bookmarks: List[str] = Field(default_factory=list)  # User IDs who bookmarked
    bookmark_count: int = Field(default=0, ge=0)
    
    # Engagement metrics
    total_engagement_score: int = Field(default=0, ge=0)  # Weighted: likes=1, comments=3, shares=5
    
    # Metadata
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: Optional[datetime] = None
    
    class Settings:
        name = "social_interactions"
        indexes = [
            [("item_id", 1), ("item_type", 1)],  # Fast lookup by item
            [("total_engagement_score", -1)],  # Trending sort
            [("updated_at", -1)],  # Recent activity
        ]
    
    # ─── Like Methods ────────────────────────────────────────────────────
    
    async def toggle_like(self, user_id: str) -> dict:
        """Toggle like status. Returns {liked: bool, count: int}."""
        if user_id in self.liked_by:
            self.liked_by.remove(user_id)
            self.likes = max(0, self.likes - 1)
            liked = False
        else:
            self.liked_by.append(user_id)
            self.likes += 1
            liked = True
        
        await self._recalculate_score()
        await self.save()
        return {"liked": liked, "count": self.likes}
    
    async def has_liked(self, user_id: str) -> bool:
        """Check if user has liked."""
        return user_id in self.liked_by
    
    # ─── Comment Methods ───────────────────────────────────────────────────
    
    async def add_comment(self, user_id: str, user_name: str, content: str, 
                         user_avatar: Optional[str] = None) -> Comment:
        """Add a top-level comment."""
        comment = Comment(
            user_id=user_id,
            user_name=user_name,
            user_avatar_url=user_avatar,
            content=content
        )
        self.comments.append(comment)
        self.comment_count = len([c for c in self.comments if not c.is_deleted])
        await self._recalculate_score()
        await self.save()
        return comment
    
    async def add_reply(self, comment_id: str, user_id: str, user_name: str, 
                       content: str, user_avatar: Optional[str] = None) -> Optional[CommentReply]:
        """Add a reply to a comment."""
        for comment in self.comments:
            if comment.id == comment_id and not comment.is_deleted:
                reply = CommentReply(
                    user_id=user_id,
                    user_name=user_name,
                    user_avatar_url=user_avatar,
                    content=content
                )
                comment.replies.append(reply)
                self.comment_count = len([
                    c for c in self.comments if not c.is_deleted
                ]) + sum(
                    len([r for r in c.replies if not r.is_deleted]) 
                    for c in self.comments
                )
                await self._recalculate_score()
                await self.save()
                return reply
        return None
    
    async def like_comment(self, comment_id: str, user_id: str) -> bool:
        """Like a comment."""
        for comment in self.comments:
            if comment.id == comment_id and not comment.is_deleted:
                if user_id not in comment.liked_by:
                    comment.liked_by.append(user_id)
                    comment.likes += 1
                    await self.save()
                return True
        return False
    
    async def delete_comment(self, comment_id: str, user_id: str, is_admin: bool = False) -> bool:
        """Soft delete a comment (owner or admin)."""
        for comment in self.comments:
            if comment.id == comment_id:
                if comment.user_id == user_id or is_admin:
                    comment.is_deleted = True
                    comment.deleted_at = datetime.utcnow()
                    self.comment_count = max(0, self.comment_count - 1)
                    await self._recalculate_score()
                    await self.save()
                    return True
        return False
    
    async def edit_comment(self, comment_id: str, user_id: str, new_content: str) -> bool:
        """Edit a comment."""
        for comment in self.comments:
            if comment.id == comment_id and comment.user_id == user_id:
                if not comment.is_deleted:
                    comment.content = new_content
                    comment.is_edited = True
                    comment.edited_at = datetime.utcnow()
                    await self.save()
                    return True
        return False
    
    # ─── Share Methods ───────────────────────────────────────────────────
    
    async def record_share(self, platform: str, user_id: Optional[str] = None) -> dict:
        """Record a share action."""
        share = ShareRecord(user_id=user_id, platform=platform)
        self.shares.append(share)
        self.share_count += 1
        await self._recalculate_score()
        await self.save()
        return {"platform": platform, "total_shares": self.share_count}
    
    # ─── Bookmark Methods ────────────────────────────────────────────────
    
    async def toggle_bookmark(self, user_id: str) -> dict:
        """Toggle bookmark status."""
        if user_id in self.bookmarks:
            self.bookmarks.remove(user_id)
            self.bookmark_count = max(0, self.bookmark_count - 1)
            bookmarked = False
        else:
            self.bookmarks.append(user_id)
            self.bookmark_count += 1
            bookmarked = True
        
        await self.save()
        return {"bookmarked": bookmarked, "count": self.bookmark_count}
    
    # ─── Internal ────────────────────────────────────────────────────────
    
    async def _recalculate_score(self):
        """Recalculate engagement score."""
        active_comments = len([c for c in self.comments if not c.is_deleted])
        active_replies = sum(
            len([r for r in c.replies if not r.is_deleted])
            for c in self.comments
        )
        self.total_engagement_score = (
            self.likes * 1 +
            (active_comments + active_replies) * 3 +
            self.share_count * 5
        )
        self.updated_at = datetime.utcnow()
    
    # ─── Class Methods ───────────────────────────────────────────────────
    
    @classmethod
    async def get_or_create(cls, item_id: str, item_type: str) -> "SocialInteraction":
        """Get existing or create new interaction document."""
        interaction = await cls.find_one({
            "item_id": item_id,
            "item_type": item_type
        })
        
        if not interaction:
            interaction = cls(item_id=item_id, item_type=item_type)
            await interaction.insert()
        
        return interaction
    
    @classmethod
    async def get_trending(cls, item_type: Optional[str] = None, 
                          limit: int = 10, hours: int = 24) -> List["SocialInteraction"]:
        """Get trending items by engagement score."""
        from datetime import timedelta
        
        cutoff = datetime.utcnow() - timedelta(hours=hours)
        query = {"updated_at": {"$gte": cutoff}}
        
        if item_type:
            query["item_type"] = item_type
        
        return await cls.find(query).sort(
            [("total_engagement_score", -1)]
        ).limit(limit).to_list()
    
    @classmethod
    async def get_user_engagement(cls, user_id: str) -> dict:
        """Get user's total engagement stats."""
        pipeline = [
            {"$match": {"liked_by": user_id}},
            {"$group": {
                "_id": None,
                "items_liked": {"$sum": 1},
                "total_likes_given": {"$sum": 1}
            }}
        ]
        
        liked_result = await cls.aggregate(pipeline).to_list(length=1)
        
        comment_pipeline = [
            {"$unwind": "$comments"},
            {"$match": {"comments.user_id": user_id, "comments.is_deleted": False}},
            {"$group": {
                "_id": None,
                "comments_made": {"$sum": 1},
                "comment_likes_received": {"$sum": "$comments.likes"}
            }}
        ]
        
        comment_result = await cls.aggregate(comment_pipeline).to_list(length=1)
        
        return {
            "items_liked": liked_result[0]["items_liked"] if liked_result else 0,
            "comments_made": comment_result[0]["comments_made"] if comment_result else 0,
            "comment_likes_received": comment_result[0]["comment_likes_received"] if comment_result else 0
        }


# ─── Request/Response Schemas ───────────────────────────────────────────

class LikeToggle(BaseModel):
    """Schema for like toggle."""
    item_id: str
    item_type: str = Field(..., pattern="^(review|product|post|menu_item|order)$")


class CommentCreate(BaseModel):
    """Schema for creating a comment."""
    item_id: str
    item_type: str = Field(..., pattern="^(review|product|post|menu_item|order)$")
    content: str = Field(..., min_length=1, max_length=2000)
    parent_comment_id: Optional[str] = None  # For replies


class CommentEdit(BaseModel):
    """Schema for editing a comment."""
    content: str = Field(..., min_length=1, max_length=2000)


class ShareRecordCreate(BaseModel):
    """Schema for recording a share."""
    item_id: str
    item_type: str = Field(..., pattern="^(review|product|post|menu_item|order)$")
    platform: str = Field(..., pattern="^(copy|twitter|facebook|whatsapp|email|native|other)$")


class BookmarkToggle(BaseModel):
    """Schema for bookmark toggle."""
    item_id: str
    item_type: str = Field(..., pattern="^(review|product|post|menu_item|order)$")
