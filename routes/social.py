# ============================================================
# Imports
# ============================================================

from datetime import datetime
from typing import Optional

from fastapi import (
    APIRouter,
    Depends,
    HTTPException,
    Query,
)

from dependencies import get_current_user

from models.user import User
from models.social_interaction import (
    SocialInteraction,
    LikeToggle,
    CommentCreate,
    CommentEdit,
    ShareRecordCreate,
    BookmarkToggle,
)
import logging
from models.notification import AppNotification, NotificationType, NotificationTarget
from models.menu import MenuItem

logger = logging.getLogger(__name__)
# ============================================================
# Router
# ============================================================

router = APIRouter(
    prefix="/social",
    tags=["Social"],
)

# ============================================================
# Helpers
# ============================================================
async def _resolve_item_label(item_id: str, item_type: str) -> str:
    """Human-readable label for the item the comment lives on."""
    if item_type == "menu_item":
        try:
            item = await MenuItem.get(item_id)
            if item:
                return f'"{item.name}"'
        except Exception:
            pass
    # fallback: "Menu Item", "Order", etc.
    return item_type.replace("_", " ").title()
    
def get_user_id(user: User) -> str:
    return str(user.id)


def map_comment(comment, user_id: Optional[str] = None):
    return {
        "id": comment.id,
        "user_id": comment.user_id,
        "user_name": comment.user_name,
        "user_avatar_url": comment.user_avatar_url,
        "content": comment.content,
        "likes": comment.likes,
        "liked_by": comment.liked_by,
        "created_at": comment.created_at.isoformat(),
        "is_edited": comment.is_edited,
        "user_liked": (
            user_id in comment.liked_by
            if user_id
            else False
        ),
    }


async def get_interaction(
    item_id: str,
    item_type: str,
):
    return await SocialInteraction.get_or_create(
        item_id,
        item_type,
    )


async def find_interaction_by_comment(
    comment_id: str,
):
    return await SocialInteraction.find_one(
        {"comments.id": comment_id}
    )

# ============================================================
# Likes
# ============================================================

@router.post("/like")
async def toggle_like(
    data: LikeToggle,
    user: User = Depends(get_current_user),
):
    interaction = await get_interaction(
        data.item_id,
        data.item_type,
    )

    result = await interaction.toggle_like(
        get_user_id(user)
    )

    return {
        "liked": result["liked"],
        "like_count": result["count"],
        "item_id": data.item_id,
    }

# ============================================================
# Comments
# ============================================================

@router.post("/comment")
async def add_comment(
    data: CommentCreate,
    user: User = Depends(get_current_user),
):
    interaction = await get_interaction(
        data.item_id,
        data.item_type,
    )

    uid = get_user_id(user)

    if data.parent_comment_id:

        reply = await interaction.add_reply(
            data.parent_comment_id,
            uid,
            user.full_name or "User",
            data.content,
            user.picture,
        )

        if not reply:
            raise HTTPException(
                status_code=404,
                detail="Parent comment not found",
            )

        return {
            "reply": reply.model_dump(),
            "item_id": data.item_id,
        }

    comment = await interaction.add_comment(
        uid,
        user.full_name or "User",
        data.content,
        user.picture,
    )

    return {
        "comment": map_comment(comment, uid),
        "item_id": data.item_id,
    }


@router.get("/comments/{item_id}")
async def get_comments(
    item_id: str,
    item_type: str = Query(...),
    page: int = 1,
    limit: int = 20,
    user: User = Depends(get_current_user),
):
    interaction = await SocialInteraction.find_one({
        "item_id": item_id,
        "item_type": item_type,
    })

    if not interaction:
        return {
            "comments": [],
            "pagination": {
                "page": page,
                "limit": limit,
                "total": 0,
                "pages": 0,
            },
            "total_comments": 0,
        }

    comments = [
        c
        for c in interaction.comments
        if not c.is_deleted
    ]

    comments.sort(
        key=lambda x: x.created_at,
        reverse=True,
    )

    start = (page - 1) * limit
    end = start + limit

    return {
        "comments": [
            map_comment(
                comment,
                get_user_id(user),
            )
            for comment in comments[start:end]
        ],
        "pagination": {
            "page": page,
            "limit": limit,
            "total": len(comments),
            "pages": (
                len(comments) + limit - 1
            ) // limit,
        },
        "total_comments": interaction.comment_count,
    }

# ============================================================
# Comment Actions
# ============================================================

@router.patch("/comment/{comment_id}")
async def edit_comment(
    comment_id: str,
    body: CommentEdit,
    user: User = Depends(get_current_user),
):
    interaction = await find_interaction_by_comment(
        comment_id
    )

    if not interaction:
        raise HTTPException(
            404,
            "Comment not found",
        )

    uid = get_user_id(user)

    for comment in interaction.comments:

        if comment.id != comment_id:
            continue

        if comment.user_id != uid:
            raise HTTPException(
                403,
                "Not your comment",
            )

        comment.content = body.content
        comment.is_edited = True
        comment.edited_at = datetime.utcnow()

        await interaction.save()

        return {"ok": True}

    raise HTTPException(
        404,
        "Comment not found",
    )


@router.delete("/comment/{comment_id}")
async def delete_comment(
    comment_id: str,
    user: User = Depends(get_current_user),
):
    interaction = await find_interaction_by_comment(
        comment_id
    )

    if not interaction:
        raise HTTPException(
            404,
            "Comment not found",
        )

    deleted = await interaction.delete_comment(
        comment_id,
        get_user_id(user),
    )

    if not deleted:
        raise HTTPException(
            403,
            "Not allowed",
        )

    return {
        "message": "Comment deleted",
    }




# ── REPLACE the existing like_comment endpoint ────────────────────────────

@router.post("/comment/{comment_id}/like")

async def like_comment(

    comment_id: str,

    user: User = Depends(get_current_user),

):

    interaction = await find_interaction_by_comment(comment_id)

    if not interaction:

        raise HTTPException(404, "Comment not found")



    liker_id   = get_user_id(user)

    liked      = await interaction.like_comment(comment_id, liker_id)



    # ── Notify comment owner only when LIKING (not un-liking)

    #    and never notify someone for liking their own comment

    if liked:

        for comment in interaction.comments:

            if comment.id != comment_id or comment.is_deleted:

                continue



            if comment.user_id == liker_id:

                break  # own comment — no notification



            try:

                item_label  = await _resolve_item_label(

                    interaction.item_id, interaction.item_type

                )

                snippet     = comment.content[:60] + (

                    "…" if len(comment.content) > 60 else ""

                )

                liker_name  = user.full_name or user.email



                await AppNotification(

                    title          = f"❤️ {liker_name} liked your comment",

                    message        = f'On {item_label}: "{snippet}"',

                    type           = NotificationType.INFO,

                    target         = NotificationTarget.SPECIFIC,

                    target_user_id = comment.user_id,

                    created_by     = liker_id,

                    created_by_name= liker_name,

                ).insert()



                logger.info(

                    f"Like notification → user {comment.user_id} "

                    f"from {liker_name} on {interaction.item_type}/{interaction.item_id}"

                )

            except Exception as e:

                # Never fail the like because of a notification error

                logger.warning(f"Like notification failed: {e}")

            break



    return {

        "message": "Comment liked" if liked else "Comment unliked",

        "liked"  : liked,   # ← now returned so frontend knows direction

    }

# ============================================================
# Shares
# ============================================================

@router.post("/share")
async def share(
    data: ShareRecordCreate,
    user: Optional[User] = Depends(get_current_user),
):
    interaction = await get_interaction(
        data.item_id,
        data.item_type,
    )

    result = await interaction.record_share(
        data.platform,
        get_user_id(user) if user else None,
    )

    return {
        "item_id": data.item_id,
        "total_shares": result["total_shares"],
    }

# ============================================================
# Bookmarks
# ============================================================

@router.post("/bookmark")
async def bookmark(
    data: BookmarkToggle,
    user: User = Depends(get_current_user),
):
    interaction = await get_interaction(
        data.item_id,
        data.item_type,
    )

    result = await interaction.toggle_bookmark(
        get_user_id(user)
    )

    return {
        "bookmarked": result["bookmarked"],
        "bookmark_count": result["count"],
        "item_id": data.item_id,
    }

# ============================================================
# Stats
# ============================================================

@router.get("/stats/{item_id}")
async def get_stats(
    item_id: str,
    item_type: str = "menu_item",   # ← safe default
    user: User = Depends(get_current_user),
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

    uid = get_user_id(user)

    return {
        "likes": interaction.likes,
        "comments": interaction.comment_count,
        "shares": interaction.share_count,
        "bookmarks": interaction.bookmark_count,
        "user_liked": uid in interaction.liked_by,
        "user_bookmarked": uid in interaction.bookmarks,
    }
