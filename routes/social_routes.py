# routes/social.py
"""
Generic social interaction endpoints consumed by <SocialActions />.

Item types:  menu_item | review

Public (auth required):
  GET    /social/{item_type}/{item_id}/stats             – likes, comments, shares + user state
  POST   /social/{item_type}/{item_id}/like              – toggle like
  POST   /social/{item_type}/{item_id}/bookmark          – toggle bookmark
  GET    /social/{item_type}/{item_id}/bookmarked        – is current user bookmarked?
  POST   /social/{item_type}/{item_id}/share             – track a share event
  GET    /social/{item_type}/{item_id}/comments          – paginated comment tree
  POST   /social/{item_type}/{item_id}/comments          – post top-level comment
  POST   /social/comments/{comment_id}/reply             – reply to a comment
  PUT    /social/comments/{comment_id}                   – edit own comment
  DELETE /social/comments/{comment_id}                   – delete own comment
  POST   /social/comments/{comment_id}/like              – toggle like on comment
  GET    /social/me/bookmarks                            – all bookmarks for current user

Admin:
  GET    /social/admin/stats                             – global social stats (for dashboard)
  GET    /social/admin/comments                          – all comments with filters
  PATCH  /social/admin/comments/{comment_id}/visibility  – hide / show
  DELETE /social/admin/comments/{comment_id}             – hard delete
"""
import logging
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from dependencies import get_current_user, get_current_admin_user
from models.social import Comment, CommentLike, Like, Bookmark, Share, ItemType
from models.user import User

router  = APIRouter(prefix="/social", tags=["Social"])
logger  = logging.getLogger(__name__)

VALID_TYPES = {e.value for e in ItemType}


# ── Schemas ───────────────────────────────────────────────────────────────────

class CommentIn(BaseModel):
    content: str = Field(..., min_length=1, max_length=1000)


class ShareIn(BaseModel):
    platform: str = Field(..., pattern="^(copy|twitter|facebook|native)$")


# ── Helpers ───────────────────────────────────────────────────────────────────

def _guard_type(item_type: str) -> ItemType:
    if item_type not in VALID_TYPES:
        raise HTTPException(422, f"item_type must be one of: {sorted(VALID_TYPES)}")
    return ItemType(item_type)


def _serialize_comment(c: Comment, viewer_id: str, liked_ids: set) -> dict:
    return {
        "id":           str(c.id),
        "item_id":      c.item_id,
        "item_type":    c.item_type.value,
        "user_id":      c.user_id,
        "user_name":    c.user_name,
        "user_picture": c.user_picture,
        "content":      c.content,
        "parent_id":    c.parent_id,
        "like_count":   c.like_count,
        "is_visible":   c.is_visible,
        "created_at":   c.created_at,
        "updated_at":   c.updated_at,
        "is_mine":      c.user_id == viewer_id,
        "liked":        str(c.id) in liked_ids,
        "replies":      [],   # populated by get_comments tree builder
    }


async def _build_comment_tree(
    item_type: ItemType,
    item_id:   str,
    viewer_id: str,
    limit:     int,
    offset:    int,
) -> list[dict]:
    """
    Returns top-level comments with nested replies in a single pass.
    Only top-level comments are paginated; all their replies are included.
    """
    # All visible comments for this item
    all_comments = await Comment.find(
        Comment.item_type  == item_type,
        Comment.item_id    == item_id,
        Comment.is_visible == True,
    ).sort([("created_at", 1)]).to_list()

    # Which comments has this viewer liked?
    viewer_likes = await CommentLike.find(
        CommentLike.user_id == viewer_id
    ).to_list()
    liked_ids = {cl.comment_id for cl in viewer_likes}

    # Build id → serialised dict
    by_id: dict[str, dict] = {}
    for c in all_comments:
        by_id[str(c.id)] = _serialize_comment(c, viewer_id, liked_ids)

    # Tree: attach replies to parents
    roots: list[dict] = []
    for c in all_comments:
        node = by_id[str(c.id)]
        if c.parent_id and c.parent_id in by_id:
            by_id[c.parent_id]["replies"].append(node)
        else:
            roots.append(node)

    return roots[offset: offset + limit]


async def _item_social_stats(item_type: ItemType, item_id: str, viewer_id: str) -> dict:
    like_count = await Like.find(
        Like.item_type == item_type,
        Like.item_id   == item_id,
    ).count()

    comment_count = await Comment.find(
        Comment.item_type  == item_type,
        Comment.item_id    == item_id,
        Comment.is_visible == True,
        Comment.parent_id  == None,   # top-level only
    ).count()

    share_count = await Share.find(
        Share.item_type == item_type,
        Share.item_id   == item_id,
    ).count()

    user_liked = await Like.find_one(
        Like.item_type == item_type,
        Like.item_id   == item_id,
        Like.user_id   == viewer_id,
    ) is not None

    user_bookmarked = await Bookmark.find_one(
        Bookmark.item_type == item_type,
        Bookmark.item_id   == item_id,
        Bookmark.user_id   == viewer_id,
    ) is not None

    return {
        "like_count":       like_count,
        "comment_count":    comment_count,
        "share_count":      share_count,
        "user_liked":       user_liked,
        "user_bookmarked":  user_bookmarked,
    }


# ── Stats ─────────────────────────────────────────────────────────────────────

@router.get("/{item_type}/{item_id}/stats")
async def get_stats(
    item_type:    str,
    item_id:      str,
    current_user: User = Depends(get_current_user),
):
    t = _guard_type(item_type)
    return await _item_social_stats(t, item_id, str(current_user.id))


# ── Like ──────────────────────────────────────────────────────────────────────

@router.post("/{item_type}/{item_id}/like")
async def toggle_like(
    item_type:    str,
    item_id:      str,
    current_user: User = Depends(get_current_user),
):
    t   = _guard_type(item_type)
    uid = str(current_user.id)

    existing = await Like.find_one(
        Like.item_type == t,
        Like.item_id   == item_id,
        Like.user_id   == uid,
    )

    if existing:
        await existing.delete()
        liked = False
    else:
        await Like(item_type=t, item_id=item_id, user_id=uid).insert()
        liked = True

    like_count = await Like.find(Like.item_type == t, Like.item_id == item_id).count()
    return {"liked": liked, "like_count": like_count}


# ── Bookmark ──────────────────────────────────────────────────────────────────

@router.post("/{item_type}/{item_id}/bookmark")
async def toggle_bookmark(
    item_type:    str,
    item_id:      str,
    current_user: User = Depends(get_current_user),
):
    t   = _guard_type(item_type)
    uid = str(current_user.id)

    existing = await Bookmark.find_one(
        Bookmark.item_type == t,
        Bookmark.item_id   == item_id,
        Bookmark.user_id   == uid,
    )

    if existing:
        await existing.delete()
        return {"bookmarked": False}

    await Bookmark(item_type=t, item_id=item_id, user_id=uid).insert()
    return {"bookmarked": True}


@router.get("/me/bookmarks")
async def get_my_bookmarks(
    item_type:    Optional[str] = None,
    current_user: User = Depends(get_current_user),
):
    query = {"user_id": str(current_user.id)}
    if item_type and item_type in VALID_TYPES:
        query["item_type"] = item_type

    bookmarks = await Bookmark.find(query).sort([("created_at", -1)]).to_list()
    return [
        {
            "id":         str(b.id),
            "item_type":  b.item_type.value,
            "item_id":    b.item_id,
            "created_at": b.created_at,
        }
        for b in bookmarks
    ]


# ── Share ─────────────────────────────────────────────────────────────────────

@router.post("/{item_type}/{item_id}/share")
async def track_share(
    item_type:    str,
    item_id:      str,
    body:         ShareIn,
    current_user: User = Depends(get_current_user),
):
    t = _guard_type(item_type)
    await Share(
        item_type=t,
        item_id=item_id,
        user_id=str(current_user.id),
        platform=body.platform,
    ).insert()

    share_count = await Share.find(Share.item_type == t, Share.item_id == item_id).count()
    return {"share_count": share_count}


# ── Comments ──────────────────────────────────────────────────────────────────

@router.get("/{item_type}/{item_id}/comments")
async def get_comments(
    item_type:    str,
    item_id:      str,
    limit:        int = Query(20, ge=1, le=100),
    offset:       int = Query(0, ge=0),
    current_user: User = Depends(get_current_user),
):
    t = _guard_type(item_type)
    tree = await _build_comment_tree(t, item_id, str(current_user.id), limit, offset)
    total = await Comment.find(
        Comment.item_type  == t,
        Comment.item_id    == item_id,
        Comment.is_visible == True,
        Comment.parent_id  == None,
    ).count()
    return {"comments": tree, "total": total}


@router.post("/{item_type}/{item_id}/comments", status_code=201)
async def post_comment(
    item_type:    str,
    item_id:      str,
    body:         CommentIn,
    current_user: User = Depends(get_current_user),
):
    t = _guard_type(item_type)
    comment = Comment(
        item_type=t,
        item_id=item_id,
        user_id=str(current_user.id),
        user_name=current_user.full_name or current_user.email,
        user_email=current_user.email,
        user_picture=current_user.picture,
        content=body.content.strip(),
        parent_id=None,
    )
    await comment.insert()
    logger.info(f"Comment by {current_user.email} on {item_type}/{item_id}")
    return _serialize_comment(comment, str(current_user.id), set())


@router.post("/comments/{comment_id}/reply", status_code=201)
async def reply_to_comment(
    comment_id:   str,
    body:         CommentIn,
    current_user: User = Depends(get_current_user),
):
    parent = await Comment.get(comment_id)
    if not parent or not parent.is_visible:
        raise HTTPException(404, "Comment not found")

    reply = Comment(
        item_type=parent.item_type,
        item_id=parent.item_id,
        user_id=str(current_user.id),
        user_name=current_user.full_name or current_user.email,
        user_email=current_user.email,
        user_picture=current_user.picture,
        content=body.content.strip(),
        parent_id=str(parent.id),
    )
    await reply.insert()
    return _serialize_comment(reply, str(current_user.id), set())


@router.put("/comments/{comment_id}")
async def edit_comment(
    comment_id:   str,
    body:         CommentIn,
    current_user: User = Depends(get_current_user),
):
    comment = await Comment.get(comment_id)
    if not comment:
        raise HTTPException(404, "Comment not found")
    if comment.user_id != str(current_user.id):
        raise HTTPException(403, "You can only edit your own comments")

    comment.content    = body.content.strip()
    comment.updated_at = datetime.utcnow()
    await comment.save()
    return _serialize_comment(comment, str(current_user.id), set())


@router.delete("/comments/{comment_id}", status_code=204)
async def delete_comment(
    comment_id:   str,
    current_user: User = Depends(get_current_user),
):
    comment = await Comment.get(comment_id)
    if not comment:
        raise HTTPException(404, "Comment not found")
    if comment.user_id != str(current_user.id):
        raise HTTPException(403, "You can only delete your own comments")

    # Soft-delete so replies are not orphaned
    comment.is_visible = False
    comment.content    = "[deleted]"
    comment.updated_at = datetime.utcnow()
    await comment.save()


@router.post("/comments/{comment_id}/like")
async def toggle_comment_like(
    comment_id:   str,
    current_user: User = Depends(get_current_user),
):
    comment = await Comment.get(comment_id)
    if not comment or not comment.is_visible:
        raise HTTPException(404, "Comment not found")

    uid      = str(current_user.id)
    existing = await CommentLike.find_one(
        CommentLike.comment_id == comment_id,
        CommentLike.user_id    == uid,
    )

    if existing:
        await existing.delete()
        comment.like_count = max(0, comment.like_count - 1)
        liked = False
    else:
        await CommentLike(comment_id=comment_id, user_id=uid).insert()
        comment.like_count += 1
        liked = True

    await comment.save()
    return {"liked": liked, "like_count": comment.like_count}


# ── Admin ─────────────────────────────────────────────────────────────────────

@router.get("/admin/stats")
async def admin_social_stats(admin: User = Depends(get_current_admin_user)):
    """Aggregate social stats for the admin dashboard widget."""
    total_likes     = await Like.count()
    total_comments  = await Comment.count()
    total_bookmarks = await Bookmark.count()
    total_shares    = await Share.count()
    hidden_comments = await Comment.find(Comment.is_visible == False).count()

    # Share platform breakdown
    all_shares = await Share.find_all().to_list()
    platforms: dict[str, int] = {}
    for s in all_shares:
        platforms[s.platform] = platforms.get(s.platform, 0) + 1

    # Most active commenters
    all_comments = await Comment.find_all().to_list()
    commenter_counts: dict[str, dict] = {}
    for c in all_comments:
        if c.user_id not in commenter_counts:
            commenter_counts[c.user_id] = {"user_name": c.user_name, "count": 0}
        commenter_counts[c.user_id]["count"] += 1

    top_commenters = sorted(
        [{"user_id": uid, **info} for uid, info in commenter_counts.items()],
        key=lambda x: x["count"],
        reverse=True,
    )[:5]

    # Most liked items
    all_likes = await Like.find_all().to_list()
    item_like_counts: dict[str, int] = {}
    for like in all_likes:
        key = f"{like.item_type.value}:{like.item_id}"
        item_like_counts[key] = item_like_counts.get(key, 0) + 1

    most_liked_items = sorted(
        [{"item": k, "like_count": v} for k, v in item_like_counts.items()],
        key=lambda x: x["like_count"],
        reverse=True,
    )[:5]

    # Recent comments
    recent = await Comment.find_all().sort([("created_at", -1)]).limit(10).to_list()

    return {
        "total_likes":       total_likes,
        "total_comments":    total_comments,
        "total_bookmarks":   total_bookmarks,
        "total_shares":      total_shares,
        "hidden_comments":   hidden_comments,
        "share_platforms":   platforms,
        "top_commenters":    top_commenters,
        "most_liked_items":  most_liked_items,
        "recent_comments": [
            {
                "id":         str(c.id),
                "item_type":  c.item_type.value,
                "item_id":    c.item_id,
                "user_name":  c.user_name,
                "user_email": c.user_email,
                "content":    c.content[:120],
                "is_visible": c.is_visible,
                "created_at": c.created_at,
            }
            for c in recent
        ],
    }


@router.get("/admin/comments")
async def admin_list_comments(
    item_type:  Optional[str]  = None,
    item_id:    Optional[str]  = None,
    visible:    Optional[bool] = None,
    limit:      int = 100,
    admin: User = Depends(get_current_admin_user),
):
    query = {}
    if item_type and item_type in VALID_TYPES: query["item_type"] = item_type
    if item_id:                                query["item_id"]   = item_id
    if visible is not None:                    query["is_visible"] = visible

    comments = await Comment.find(query).sort([("created_at", -1)]).limit(limit).to_list()
    return [
        {
            "id":         str(c.id),
            "item_type":  c.item_type.value,
            "item_id":    c.item_id,
            "user_name":  c.user_name,
            "user_email": c.user_email,
            "content":    c.content,
            "parent_id":  c.parent_id,
            "like_count": c.like_count,
            "is_visible": c.is_visible,
            "created_at": c.created_at,
        }
        for c in comments
    ]


@router.patch("/admin/comments/{comment_id}/visibility")
async def toggle_comment_visibility(
    comment_id: str,
    admin: User = Depends(get_current_admin_user),
):
    comment = await Comment.get(comment_id)
    if not comment:
        raise HTTPException(404, "Comment not found")
    comment.is_visible = not comment.is_visible
    await comment.save()
    action = "shown" if comment.is_visible else "hidden"
    logger.info(f"Admin {admin.email} {action} comment {comment_id}")
    return {"is_visible": comment.is_visible}


@router.delete("/admin/comments/{comment_id}", status_code=204)
async def admin_delete_comment(
    comment_id: str,
    admin: User = Depends(get_current_admin_user),
):
    comment = await Comment.get(comment_id)
    if not comment:
        raise HTTPException(404, "Comment not found")
    await comment.delete()
    # Also delete all replies
    await Comment.find(Comment.parent_id == comment_id).delete()
    logger.info(f"Admin {admin.email} hard-deleted comment {comment_id} + replies")
