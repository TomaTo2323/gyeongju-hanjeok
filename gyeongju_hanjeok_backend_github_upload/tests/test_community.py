from pathlib import Path
from tempfile import TemporaryDirectory

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.community_api import (
    CommentCreate,
    CommunityPostCreate,
    CommunityPostType,
    _crowd_bucket,
    _snapshot_place_ids,
    create_comment,
    create_post,
    get_course_copy_seed,
    live_summary,
    recommend_post,
    save_course_from_post,
)
from app.db import Base, PlaceRecord, UserRecord


def test_crowd_bucket_boundaries_match_home_scale():
    assert _crowd_bucket(0).key == "very_quiet"
    assert _crowd_bucket(19).color_key == "blue"
    assert _crowd_bucket(20).color_key == "green"
    assert _crowd_bucket(40).color_key == "yellow"
    assert _crowd_bucket(60).color_key == "crimson"
    assert _crowd_bucket(80).color_key == "red"
    assert _crowd_bucket(100).key == "very_busy"


def test_snapshot_place_ids_supports_backend_and_flutter_shapes():
    assert _snapshot_place_ids(
        {"places": [{"place_id": "1"}, {"place_id": "2"}]}
    ) == ["1", "2"]
    assert _snapshot_place_ids(
        {"stops": [{"place": {"id": "3"}}, {"place": {"place_id": "4"}}]}
    ) == ["3", "4"]


def test_community_course_and_live_flow():
    with TemporaryDirectory() as temp_dir:
        engine = create_engine(
            f"sqlite:///{Path(temp_dir) / 'community.db'}",
            connect_args={"check_same_thread": False},
        )
        Base.metadata.create_all(engine)
        Session = sessionmaker(bind=engine, expire_on_commit=False)
        db = Session()
        try:
            user = UserRecord(
                email="community@test.local",
                password_hash="test",
                nickname="테스터",
            )
            place = PlaceRecord(
                place_id="p1",
                title="첨성대",
                category="관광지",
                latitude=35.8,
                longitude=129.2,
                data={"congestion_score": 32.0},
            )
            db.add_all([user, place])
            db.commit()
            db.refresh(user)

            course_post = create_post(
                CommunityPostCreate(
                    post_type=CommunityPostType.course,
                    content="직접 다녀온 코스예요.",
                    course_rating=4.5,
                    course_snapshot={
                        "course_id": "c1",
                        "title": "한적 코스",
                        "places": [{"place_id": "p1", "title": "첨성대"}],
                    },
                ),
                user,
                db,
            )
            recommendation = recommend_post(course_post.post_id, user, db)
            comment = create_comment(
                course_post.post_id,
                CommentCreate(content="좋아요!"),
                user,
                db,
            )
            saved = save_course_from_post(course_post.post_id, user, db)
            copied = get_course_copy_seed(course_post.post_id, user, db)

            assert recommendation.recommendation_count == 1
            assert comment.post_id == course_post.post_id
            assert saved.source_post_id == course_post.post_id
            assert copied.place_ids == ["p1"]
            assert copied.source_place_names == ["첨성대"]
            assert copied.same_course_request_patch["required_place_names"] == ["첨성대"]
            assert copied.current_places[0].found is True

            create_post(
                CommunityPostCreate(
                    post_type=CommunityPostType.live,
                    content="지금 사진 찍기 좋아요.",
                    place_id="p1",
                    crowd_percent=25,
                ),
                user,
                db,
            )
            summary = live_summary("p1", 2, user, db)
            assert summary.report_count == 1
            assert summary.community_average_percent == 25.0
            assert summary.community_bucket is not None
            assert summary.community_bucket.key == "quiet"
        finally:
            db.close()
