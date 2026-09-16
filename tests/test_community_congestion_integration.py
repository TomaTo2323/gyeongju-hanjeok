from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import app.services as services
from app.db import Base, CommunityPostRecord, PlaceRecord, UserRecord
from app.schemas import Place


def _place(score: float = 60.0) -> Place:
    return Place(
        place_id="p1",
        title="첨성대",
        category="관광지",
        latitude=35.8347,
        longitude=129.2191,
        congestion_score=score,
    )


def test_community_signal_never_overwrites_v244_score():
    place = _place(60.0)
    now = datetime.now(timezone.utc)

    services._apply_community_live_signal(
        place,
        {
            "score": 90.0,
            "count": 4,
            "latest_observed_at": now,
            "influence": 0.20,
        },
    )

    # 원본 V2.4.4 결과는 그대로 유지한다.
    assert place.congestion_score == 60.0
    # 코스용 혼잡도만 최대 20% 보정한다: 60*0.8 + 90*0.2 = 66.
    assert place.routing_congestion_score == 66.0
    assert place.community_congestion_score == 90.0
    assert place.community_report_count == 4


def test_recent_live_reports_are_freshness_weighted(monkeypatch):
    with TemporaryDirectory() as temp_dir:
        engine = create_engine(
            f"sqlite:///{Path(temp_dir) / 'community_signal.db'}",
            connect_args={"check_same_thread": False},
        )
        Base.metadata.create_all(engine)
        Session = sessionmaker(bind=engine, expire_on_commit=False)
        monkeypatch.setattr(services, "SessionLocal", Session)

        db = Session()
        try:
            user = UserRecord(
                email="signal@test.local",
                password_hash="test",
                nickname="현장제보자",
            )
            place = PlaceRecord(
                place_id="p1",
                title="첨성대",
                category="관광지",
                latitude=35.8,
                longitude=129.2,
                data={},
            )
            db.add_all([user, place])
            db.commit()
            db.refresh(user)

            now = datetime.now(timezone.utc)
            db.add_all(
                [
                    CommunityPostRecord(
                        author_user_id=user.user_id,
                        post_type="live",
                        title="지금 혼잡",
                        content="사람이 많아요",
                        place_id="p1",
                        crowd_percent=90,
                        observed_at=now - timedelta(minutes=5),
                    ),
                    CommunityPostRecord(
                        author_user_id=user.user_id,
                        post_type="live",
                        title="조금 전 한적",
                        content="아까는 한적했어요",
                        place_id="p1",
                        crowd_percent=20,
                        observed_at=now - timedelta(minutes=100),
                    ),
                    # 2시간 창 밖 제보는 계산에서 제외된다.
                    CommunityPostRecord(
                        author_user_id=user.user_id,
                        post_type="live",
                        title="오래된 제보",
                        content="예전 정보",
                        place_id="p1",
                        crowd_percent=0,
                        observed_at=now - timedelta(hours=3),
                    ),
                ]
            )
            db.commit()

            signals = services._community_live_signal_map(["p1"], now=now)
            signal = signals["p1"]

            assert signal["count"] == 2
            # 최신 90% 제보가 100분 전 20% 제보보다 더 큰 가중치를 가진다.
            assert float(signal["score"]) > 70.0
            assert 0.0 < float(signal["influence"]) <= services.COMMUNITY_LIVE_MAX_INFLUENCE
        finally:
            db.close()
