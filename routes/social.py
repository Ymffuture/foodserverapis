# backend/routes/social.py
from datetime import datetime
from typing import Optional, List
from fastapi import APIRouter, Depends, HTTPException, Query, status, BackgroundTasks

from models.social_interaction import (
    SocialInteraction, LikeToggle, CommentCreate, CommentEdit,
    ShareRecordCreate, BookmarkToggle
)
from models.user import User
from auth.dependencies import get_current_active_user, require_admin

router = APIRouter(prefix="/social", tags=["Social"])


# ═══════════════════════════════════════════════════════════════════════
# LIKE ROUTES
# ═══════════════════════════════════════════════════════════════════════

@router.post("/like", response_model=dict)
async def toggle_like(
    data: LikeToggle,
    current_user: User = Depends(get_current_active_user)
):
    """Toggle like on an item."""
    interaction = await SocialInteraction.get_or_create(
        data.item_id, data.item_type
    )
    
    result = await interaction.toggle_like(str(current_user.id))
    
    return {
        "message": "Like toggled",
        "liked": result["liked"],
        "like_count": result["count"],
        "item_id": data.item_id
    }


@router.get("/like/{item_id}", response_model=dict)
async def get_like_status(
    item_id: str,
    item_type: str = Query(..., pattern="^(review|product|post|menu_item|order)$"),
    current_user: Optional[User] = Depends(get_current_active_user)
):
    """Get like status and count for an item."""
    interaction = await SocialInteraction.find_one({
        "item_id": item_id,
        "item_type": item_type
    })
    
    if not interaction:
        return {
            "item_id": item_id,
            "like_count": 0,
            "user_liked": False
        }
    
    user_liked = False
    if current_user:
        user_liked = await interaction.has_liked(str(current_user.id))
    
    return {
        "item_id": item_id,
        "like_count": interaction.likes,
        "user_liked": user_liked,
        "liked_by_sample": interaction.liked_by[:5]  # First 5 likers
    }


# ═══════════════════════════════════════════════════════════════════════
# COMMENT ROUTES
# ═══════════════════════════════════════════════════════════════════════

@router.post("/comment", status_code=status.HTTP_201_CREATED, response_model=dict)
async def add_comment(
    data: CommentCreate,
    current_user: User = Depends(get_current_active_user)
):
    """Add a comment or reply to an item."""
    interaction = await SocialInteraction.get_or_create(
        data.item_id, data.item_type
    )
    
    if data.parent_comment_id:
        # It's a reply
        reply = await interaction.add_reply(
            data.parent_comment_id,
            str(current_user.id),
            current_user.name or current_user.email.split("@")[0],
            data.content,
            getattr(current_user, "avatar_url", None)
        )
        
        if not reply:
            raise HTTPException(status_code=404, detail="Parent comment not found")
        
        return {
            "message": "Reply added",
            "reply": reply.model_dump(),
            "item_id": data.item_id,
            "total_comments": interaction.comment_count
        }
    
    # Top-level comment
    comment = await interaction.add_comment(
        str(current_user.id),
        current_user.name or current_user.email.split("@")[0],
        data.content,
        getattr(current_user, "avatar_url", None)
    )
    
    return {
        "message": "Comment added",
        "comment": comment.model_dump(),
        "item_id": data.item_id,
        "total_comments": interaction.comment_count
    }


@router.get("/comments/{item_id}", response_model=dict)
async def get_comments(
    item_id: str,
    item_type: str = Query(..., pattern="^(review|product|post|menu_item|order)$"),
    sort: str = Query("newest", pattern="^(newest|oldest|popular)$"),
    page: int = Query(1, ge=1),
    limit: int = Query(20, ge=1, le=100),
    current_user: Optional[User] = Depends(get_current_active_user)
):
    """Get comments for an item with pagination."""
    interaction = await SocialInteraction.find_one({
        "item_id": item_id,
        "item_type": item_type
    })
    
    if not interaction:
        return {
            "item_id": item_id,
            "comments": [],
            "pagination": {"page": 1, "limit": limit, "total": 0, "pages": 0}
        }
    
    # Filter deleted comments
    comments = [
        c for c in interaction.comments 
        if not c.is_deleted
    ]
    
    # Sort
    if sort == "newest":
        comments.sort(key=lambda x: x.created_at, reverse=True)
    elif sort == "oldest":
        comments.sort(key=lambda x: x.created_at)
    elif sort == "popular":
        comments.sort(key=lambda x: x.likes, reverse=True)
    
    # Pagination
    total = len(comments)
    start = (page - 1) * limit
    end = start + limit
    paginated = comments[start:end]
    
    # Add user-specific data
    for comment in paginated:
        comment_data = comment.model_dump()
        if current_user:
            comment_data["user_liked"] = str(current_user.id) in comment.liked_by
        else:
            comment_data["user_liked"] = False
    
    return {
        "item_id": item_id,
        "comments": [c.model_dump() for c in paginated],
        "pagination": {
            "page": page,
            "limit": limit,
            "total": total,
            "pages": (total + limit - 1) // limit
        },
        "total_comments": interaction.comment_count
    }


@router.put("/comment/{comment_id}", response_model=dict)
async def edit_comment(
    comment_id: str,
    data: CommentEdit,
    current_user: User = Depends(get_current_active_user)
):
    """Edit own comment."""
    # Find the interaction containing this comment
    interaction = await SocialInteraction.find_one({
        "comments.id": comment_id
    })
    
    if not interaction:
        raise HTTPException(status_code=404, detail="Comment not found")
    
    success = await interaction.edit_comment(
        comment_id, str(current_user.id), data.content
    )
    
    if not success:
        raise HTTPException(status_code=403, detail="Can only edit your own comments")
    
    return {"message": "Comment updated"}


@router.delete("/comment/{comment_id}")
async def delete_comment(
    comment_id: str,
    current_user: User = Depends(get_current_active_user)
):
    """Delete own comment."""
    interaction = await SocialInteraction.find_one({
        "comments.id": comment_id
    })
    
    if not interaction:
        raise HTTPException(status_code=404, detail="Comment not found")
    
    success = await interaction.delete_comment(
        comment_id, str(current_user.id)
    )
    
    if not success:
        raise HTTPException(status_code=403, detail="Can only delete your own comments")
    
    return {
        "message": "Comment deleted",
        "total_comments": interaction.comment_count
    }


@router.post("/comment/{comment_id}/like", response_model=dict)
async def like_comment(
    comment_id: str,
    current_user: User = Depends(get_current_active_user)
):
    """Like a comment."""
    interaction = await SocialInteraction.find_one({
        "comments.id": comment_id
    })
    
    if not interaction:
        raise HTTPException(status_code=404, detail="Comment not found")
    
    success = await interaction.like_comment(comment_id, str(current_user.id))
    
    if not success:
        raise HTTPException(status_code=404, detail="Comment not found")
    
    return {"message": "Comment liked"}


# ═══════════════════════════════════════════════════════════════════════
# SHARE ROUTES
# ═══════════════════════════════════════════════════════════════════════

@router.post("/share", status_code=status.HTTP_201_CREATED, response_model=dict)
async def record_share(
    data: ShareRecordCreate,
    current_user: Optional[User] = Depends(get_current_active_user)
):
    """Record a share action."""
    interaction = await SocialInteraction.get_or_create(
        data.item_id, data.item_type
    )
    
    user_id = str(current_user.id) if current_user else None
    result = await interaction.record_share(data.platform, user_id)
    
    return {
        "message": "Share recorded",
        "platform": data.platform,
        "total_shares": result["total_shares"],
        "item_id": data.item_id
    }


# ═══════════════════════════════════════════════════════════════════════
# BOOKMARK ROUTES
# ═══════════════════════════════════════════════════════════════════════

@router.post("/bookmark", response_model=dict)
async def toggle_bookmark(
    data: BookmarkToggle,
    current_user: User = Depends(get_current_active_user)
):
    """Toggle bookmark on an item."""
    interaction = await SocialInteraction.get_or_create(
        data.item_id, data.item_type
    )
    
    result = await interaction.toggle_bookmark(str(current_user.id))
    
    return {
        "message": "Bookmark toggled",
        "bookmarked": result["bookmarked"],
        "bookmark_count": result["count"],
        "item_id": data.item_id
    }


@router.get("/bookmarks", response_model=dict)
async def get_my_bookmarks(
    current_user: User = Depends(get_current_active_user),
    page: int = Query(1, ge=1),
    limit: int = Query(20, ge=1, le=100)
):
    """Get current user's bookmarked items."""
    skip = (page - 1) * limit
    
    interactions = await SocialInteraction.find({
        "bookmarks": str(current_user.id)
    }).sort([("updated_at", -1)]).skip(skip).limit(limit).to_list()
    
    total = await SocialInteraction.find({
        "bookmarks": str(current_user.id)
    }).count()
    
    return {
        "bookmarks": [
            {
                "item_id": i.item_id,
                "item_type": i.item_type,
                "engagement": {
                    "likes": i.likes,
                    "comments": i.comment_count,
                    "shares": i.share_count
                }
            }
            for i in interactions
        ],
        "pagination": {
            "page": page,
            "limit": limit,
            "total": total,
            "pages": (total + limit - 1) // limit
        }
    }


# ═══════════════════════════════════════════════════════════════════════
# TRENDING / ANALYTICS
# ═══════════════════════════════════════════════════════════════════════

@router.get("/trending", response_model=dict)
async def get_trending(
    item_type: Optional[str] = Query(None, pattern="^(review|product|post|menu_item|order)$"),
    hours: int = Query(24, ge=1, le=168),
    limit: int = Query(10, ge=1, le=50)
):
    """Get trending items by engagement."""
    trending = await SocialInteraction.get_trending(item_type, limit, hours)
    
    return {
        "trending": [
            {
                "item_id": t.item_id,
                "item_type": t.item_type,
                "engagement": {
                    "likes": t.likes,
                    "comments": t.comment_count,
                    "shares": t.share_count,
                    "score": t.total_engagement_score
                },
                "updated_at": t.updated_at
            }
            for t in trending
        ],
        "time_window_hours": hours
    }


@router.get("/stats/{item_id}", response_model=dict)
async def get_item_stats(
    item_id: str,
    item_type: str = Query(..., pattern="^(review|product|post|menu_item|order)$"),
    current_user: Optional[User] = Depends(get_current_active_user)
):
    """Get full engagement stats for an item."""
    interaction = await SocialInteraction.find_one({
        "item_id": item_id,
        "item_type": item_type
    })
    
    if not interaction:
        return {
            "item_id": item_id,
            "likes": 0,
            "comments": 0,
            "shares": 0,
            "bookmarks": 0,
            "user_liked": False,
            "user_bookmarked": False
        }
    
    user_id = str(current_user.id) if current_user else None
    
    return {
        "item_id": item_id,
        "likes": interaction.likes,
        "comments": interaction.comment_count,
        "shares": interaction.share_count,
        "bookmarks": interaction.bookmark_count,
        "engagement_score": interaction.total_engagement_score,
        "user_liked": user_id in interaction.liked_by if user_id else False,
        "user_bookmarked": user_id in interaction.bookmarks if user_id else False,
        "last_activity": interaction.updated_at
    }
