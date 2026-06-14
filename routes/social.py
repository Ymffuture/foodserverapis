# backend/routes/social.py
from fastapi import APIRouter, Depends, HTTPException, Query, status
from typing import Optional

from models.social_interaction import (
    SocialInteraction,
    LikeToggle,
    CommentCreate,
    CommentEdit,
    ShareRecordCreate,
    BookmarkToggle,
)

from models.user import User
from dependencies import get_current_user

router = APIRouter(prefix="/social", tags=["Social"])


# ─────────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────────

def map_comment(c, user_id=None):
    return {
        "id": c.id,
        "user_id": c.user_id,
        "user_name": c.user_name,
        "user_avatar_url": c.user_avatar_url,
        "content": c.content,
        "likes": c.likes,
        "liked_by": c.liked_by,
        "created_at": c.created_at.isoformat(),
        "is_edited": c.is_edited,
        "user_liked": user_id in c.liked_by if user_id else False,
    }


# ─────────────────────────────────────────────
# LIKE
# ─────────────────────────────────────────────

@router.post("/like")
async def toggle_like(data: LikeToggle, user: User = Depends(get_current_user)):
    interaction = await SocialInteraction.get_or_create(data.item_id, data.item_type)

    result = await interaction.toggle_like(str(user.id))

    return {
        "liked": result["liked"],
        "like_count": result["count"],
        "item_id": data.item_id,
    }


# ─────────────────────────────────────────────
# COMMENTS
# ─────────────────────────────────────────────

@router.post("/comment")
async def add_comment(data: CommentCreate, user: User = Depends(get_current_user)):
    interaction = await SocialInteraction.get_or_create(data.item_id, data.item_type)

    if data.parent_comment_id:
        reply = await interaction.add_reply(
            data.parent_comment_id,
            str(user.id),
            user.name or "user",
            data.content,
            getattr(user, "avatar_url", None),
        )

        if not reply:
            raise HTTPException(404, "Parent comment not found")

        return {"reply": reply.model_dump(), "item_id": data.item_id}

    comment = await interaction.add_comment(
        str(user.id),
        user.name or "user",
        data.content,
        getattr(user, "avatar_url", None),
    )

    return {
        "comment": map_comment(comment, str(user.id)),
        "item_id": data.item_id,
    }


@router.get("/comments/{item_id}")
async def get_comments(
    item_id: str,
    item_type: str = Query(...),
    page: int = 1,
    limit: int = 20,
    user: Optional[User] = Depends(get_current_user),
):
    interaction = await SocialInteraction.find_one({
        "item_id": item_id,
        "item_type": item_type
    })

    if not interaction:
        return {
            "comments": [],
            "pagination": {"page": 1, "limit": limit, "total": 0, "pages": 0},
            "total_comments": 0,
        }

    comments = [c for c in interaction.comments if not c.is_deleted]
    comments.sort(key=lambda x: x.created_at, reverse=True)

    start = (page - 1) * limit
    end = start + limit

    paginated = comments[start:end]

    return {
        "comments": [map_comment(c, str(user.id) if user else None) for c in paginated],
        "pagination": {
            "page": page,
            "limit": limit,
            "total": len(comments),
            "pages": (len(comments) + limit - 1) // limit,
        },
        "total_comments": interaction.comment_count,
    }


@router.delete("/comment/{comment_id}")
async def delete_comment(comment_id: str, user: User = Depends(get_current_user)):
    interaction = await SocialInteraction.find_one({"comments.id": comment_id})

    if not interaction:
        raise HTTPException(404, "Comment not found")

    ok = await interaction.delete_comment(comment_id, str(user.id))

    if not ok:
        raise HTTPException(403, "Not allowed")

    return {"message": "deleted"}


@router.post("/comment/{comment_id}/like")
async def like_comment(comment_id: str, user: User = Depends(get_current_user)):
    interaction = await SocialInteraction.find_one({"comments.id": comment_id})

    if not interaction:
        raise HTTPException(404, "Not found")

    await interaction.like_comment(comment_id, str(user.id))

    return {"message": "liked"}


# ─────────────────────────────────────────────
# SHARE
# ─────────────────────────────────────────────

@router.post("/share")
async def share(data: ShareRecordCreate, user: Optional[User] = Depends(get_current_user)):
    interaction = await SocialInteraction.get_or_create(data.item_id, data.item_type)

    result = await interaction.record_share(
        data.platform,
        str(user.id) if user else None,
    )

    return {
        "item_id": data.item_id,
        "total_shares": result["total_shares"],
    }


# ─────────────────────────────────────────────
# BOOKMARK
# ─────────────────────────────────────────────

@router.post("/bookmark")
async def bookmark(data: BookmarkToggle, user: User = Depends(get_current_user)):
    interaction = await SocialInteraction.get_or_create(data.item_id, data.item_type)

    result = await interaction.toggle_bookmark(str(user.id))

    return {
        "bookmarked": result["bookmarked"],
        "bookmark_count": result["count"],
        "item_id": data.item_id,
    }


# ─────────────────────────────────────────────
# STATS
# ─────────────────────────────────────────────

@router.get("/stats/{item_id}")
async def stats(
    item_id: str,
    item_type: str = Query(...),
    user: Optional[User] = Depends(get_current_user),
):
    interaction = await SocialInteraction.find_one({
        "item_id": item_id,
        "item_type": item_type,
    })

    if not interaction:
        return {
            "likes": 0,
            "comments": 0,
            "shares": 0,
            "bookmarks": 0,
            "user_liked": False,
            "user_bookmarked": False,
        }

    uid = str(user.id) if user else None

    return {
        "likes": interaction.likes,
        "comments": interaction.comment_count,
        "shares": interaction.share_count,
        "bookmarks": interaction.bookmark_count,
        "user_liked": uid in interaction.liked_by if uid else False,
        "user_bookmarked": uid in interaction.bookmarks if uid else False,
    }
