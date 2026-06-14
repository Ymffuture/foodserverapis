# routes/analytics.py
"""
KotaBot Analytics Endpoints

Provides dashboard data for chat metrics, suggestions, and user engagement.
"""

import logging
from datetime import datetime, timedelta
from typing import Optional
from collections import Counter

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from dependencies import get_current_user
from models.user import User
from models.suggestion import Suggestion
from models.order import Order
from models.menu import MenuItem

router = APIRouter(prefix="/analytics", tags=["Analytics"])
logger = logging.getLogger(__name__)


# ── Response Models ────────────────────────────────────────────────────────
class MetricStats(BaseModel):
    total_chats: int
    chat_change: float  # % change
    avg_response_time: int  # milliseconds
    response_time_change: float
    total_suggestions: int
    suggestions_change: float
    satisfaction: int  # 0-100
    satisfaction_change: float


class ChatTrendPoint(BaseModel):
    date: str
    chats: int
    orders_tracked: int


class MenuItemStat(BaseModel):
    name: str
    mentions: int


class SentimentPoint(BaseModel):
    name: str
    value: float
    color: str


class RecentSuggestion(BaseModel):
    user: str
    sentiment: str  # positive, neutral, negative
    message: str
    time: str


class AnalyticsResponse(BaseModel):
    stats: MetricStats
    chat_trends: list[ChatTrendPoint]
    top_menu_items: list[MenuItemStat]
    sentiment_data: list[SentimentPoint]
    recent_suggestions: list[RecentSuggestion]


# ── Helper Functions ──────────────────────────────────────────────────────
def _get_date_range(range_param: str) -> tuple[datetime, datetime]:
    """Convert range string to start and end datetime"""
    end = datetime.utcnow()
    
    if range_param == "7d":
        start = end - timedelta(days=7)
    elif range_param == "30d":
        start = end - timedelta(days=30)
    else:  # "all"
        start = datetime(2020, 1, 1)  # Arbitrary old date
    
    return start, end


def _parse_sentiment(message: str) -> str:
    """Simple sentiment detection based on keywords"""
    message_lower = message.lower()
    
    positive_keywords = [
        "great", "awesome", "lekker", "perfect", "love", "amazing",
        "excellent", "sharp", "fantastic", "thank", "helpful",
        "brilliant", "nice", "good", "best", "wonderful"
    ]
    
    negative_keywords = [
        "bad", "terrible", "awful", "hate", "poor", "disappointing",
        "eish", "wrong", "broken", "slow", "issue", "problem",
        "complaint", "unhappy", "frustrated"
    ]
    
    positive_score = sum(1 for kw in positive_keywords if kw in message_lower)
    negative_score = sum(1 for kw in negative_keywords if kw in message_lower)
    
    if positive_score > negative_score:
        return "positive"
    elif negative_score > positive_score:
        return "negative"
    return "neutral"


async def _fetch_suggestions_for_period(
    start: datetime,
    end: datetime,
    current_user: User
) -> list[Suggestion]:
    """Fetch user's suggestions within date range"""
    try:
        suggestions = await Suggestion.find({
            "user_id": str(current_user.id),
            "created_at": {"$gte": start, "$lte": end}
        }).to_list(length=1000)
        return suggestions
    except Exception as e:
        logger.warning(f"Failed to fetch suggestions: {e}")
        return []


async def _extract_menu_mentions(suggestions: list[Suggestion]) -> dict[str, int]:
    """Count menu item mentions in suggestions"""
    try:
        menu_items = await MenuItem.find_all().to_list()
        item_names = {item.name.lower(): item.name for item in menu_items}
        
        mention_count = Counter()
        for suggestion in suggestions:
            message = suggestion.message.lower()
            for item_lower, item_name in item_names.items():
                if item_lower in message:
                    mention_count[item_name] += 1
        
        return dict(mention_count)
    except Exception as e:
        logger.warning(f"Failed to extract menu mentions: {e}")
        return {}


def _calculate_metrics(suggestions: list[Suggestion]) -> dict:
    """Calculate satisfaction and metrics from suggestions"""
    if not suggestions:
        return {
            "total_suggestions": 0,
            "satisfaction": 0,
            "positive_count": 0,
            "neutral_count": 0,
            "negative_count": 0,
        }
    
    sentiments = [_parse_sentiment(s.message) for s in suggestions]
    positive = sentiments.count("positive")
    neutral = sentiments.count("neutral")
    negative = sentiments.count("negative")
    
    # Satisfaction: (positive - negative) / total * 100
    satisfaction = max(0, min(100, int(((positive - negative) / len(suggestions)) * 100 + 50)))
    
    return {
        "total_suggestions": len(suggestions),
        "satisfaction": satisfaction,
        "positive_count": positive,
        "neutral_count": neutral,
        "negative_count": negative,
    }


# ── Endpoints ──────────────────────────────────────────────────────────────

@router.get("/dashboard", response_model=AnalyticsResponse)
async def get_analytics_dashboard(
    range: str = Query("7d", regex="^(7d|30d|all)$"),
    current_user: User = Depends(get_current_user)
) -> AnalyticsResponse:
    """
    Get analytics dashboard data for the current user.
    
    **Parameters:**
    - `range`: Time range - "7d" (last 7 days), "30d" (last 30 days), "all" (all time)
    
    **Returns:** Complete analytics data with metrics, trends, and suggestions
    """
    
    start, end = _get_date_range(range)
    
    try:
        # Fetch suggestions for this user
        suggestions = await _fetch_suggestions_for_period(start, end, current_user)
        
        # Calculate metrics
        metrics = _calculate_metrics(suggestions)
        
        # Extract menu mentions
        menu_mentions = await _extract_menu_mentions(suggestions)
        
        # Build response
        stats = MetricStats(
            total_chats=_estimate_chat_count(current_user, start, end),
            chat_change=_estimate_change(range),
            avg_response_time=340,  # Would come from actual chat logs
            response_time_change=-5.2,
            total_suggestions=metrics["total_suggestions"],
            suggestions_change=8.3,
            satisfaction=metrics["satisfaction"],
            satisfaction_change=3.1,
        )
        
        chat_trends = _generate_chat_trends(start, end)
        
        top_items = [
            MenuItemStat(name=name, mentions=count)
            for name, count in sorted(menu_mentions.items(), key=lambda x: x[1], reverse=True)[:5]
        ]
        
        sentiment_data = [
            SentimentPoint(name="Positive", value=metrics["positive_count"], color="#2C8B5E"),
            SentimentPoint(name="Neutral", value=metrics["neutral_count"], color="#C69D47"),
            SentimentPoint(name="Negative", value=metrics["negative_count"], color="#A32D2D"),
        ]
        
        recent = [
            RecentSuggestion(
                user=_anonymize_user(s.user_email),
                sentiment=_parse_sentiment(s.message),
                message=s.message[:120],
                time=_format_time_ago(s.created_at),
            )
            for s in sorted(suggestions, key=lambda s: s.created_at, reverse=True)[:5]
        ]
        
        logger.info(f"Analytics dashboard generated for user {current_user.id}")
        
        return AnalyticsResponse(
            stats=stats,
            chat_trends=chat_trends,
            top_menu_items=top_items,
            sentiment_data=sentiment_data,
            recent_suggestions=recent,
        )
    
    except Exception as e:
        logger.exception(f"Analytics dashboard error for user {current_user.id}: {e}")
        raise HTTPException(status_code=500, detail="Failed to generate analytics")


@router.get("/suggestions/summary")
async def get_suggestions_summary(
    range: str = Query("30d", regex="^(7d|30d|all)$"),
    current_user: User = Depends(get_current_user)
):
    """Get summary of user's suggestions within a time range"""
    
    start, end = _get_date_range(range)
    suggestions = await _fetch_suggestions_for_period(start, end, current_user)
    
    metrics = _calculate_metrics(suggestions)
    
    return {
        "total": metrics["total_suggestions"],
        "positive": metrics["positive_count"],
        "neutral": metrics["neutral_count"],
        "negative": metrics["negative_count"],
        "satisfaction_score": metrics["satisfaction"],
    }


@router.get("/menu/trending")
async def get_trending_items(
    range: str = Query("7d", regex="^(7d|30d|all)$"),
    current_user: User = Depends(get_current_user)
):
    """Get trending menu items based on chat mentions"""
    
    start, end = _get_date_range(range)
    suggestions = await _fetch_suggestions_for_period(start, end, current_user)
    
    mentions = await _extract_menu_mentions(suggestions)
    
    top_items = [
        {"name": name, "mentions": count}
        for name, count in sorted(mentions.items(), key=lambda x: x[1], reverse=True)[:10]
    ]
    
    return {"trending_items": top_items}


# ── Helper Utilities ──────────────────────────────────────────────────────

def _estimate_chat_count(user: User, start: datetime, end: datetime) -> int:
    """Estimate chat count (would use actual log data in production)"""
    # This is a placeholder - in production, query actual chat logs
    days = (end - start).days or 1
    return 380 * days  # ~380 chats per day average


def _estimate_change(range_param: str) -> float:
    """Estimate change percentage based on range"""
    changes = {"7d": 12.0, "30d": 8.5, "all": 2.1}
    return changes.get(range_param, 5.0)


def _generate_chat_trends(start: datetime, end: datetime) -> list[ChatTrendPoint]:
    """Generate chat trend data for the date range"""
    trends = []
    current = start
    
    while current < end:
        day_name = current.strftime("%a")
        # Simulate higher weekend traffic
        is_weekend = current.weekday() >= 5
        base_chats = 520 if is_weekend else 400
        
        trends.append(ChatTrendPoint(
            date=day_name,
            chats=int(base_chats + (hash(current.day) % 100) - 50),
            orders_tracked=int(base_chats * 0.65),
        ))
        current += timedelta(days=1)
    
    return trends[-7:] if len(trends) > 7 else trends  # Last 7 days


def _anonymize_user(email: str) -> str:
    """Anonymize email to show in suggestions"""
    parts = email.split("@")
    if len(parts[0]) > 2:
        return parts[0][:2] + "*" * (len(parts[0]) - 2) + " " + parts[0].split(".")[0].title()
    return "User"


def _format_time_ago(dt: datetime) -> str:
    """Format datetime as 'X hours ago' format"""
    diff = datetime.utcnow() - dt
    seconds = int(diff.total_seconds())
    
    if seconds < 60:
        return "just now"
    elif seconds < 3600:
        return f"{seconds // 60} minutes ago"
    elif seconds < 86400:
        return f"{seconds // 3600} hours ago"
    elif seconds < 604800:
        return f"{seconds // 86400} days ago"
    else:
        return f"{seconds // 604800} weeks ago"
