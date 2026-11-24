"""Exercise Mixtape's reported behavior through its Flask endpoints."""

from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest

from app import create_app, db
from models import ListeningEvent, Playlist, Song, Tag, User, friendships, playlist_entries


@pytest.fixture
def sample():
    app = create_app({"TESTING": True, "SQLALCHEMY_DATABASE_URI": "sqlite:///:memory:"})
    with app.app_context():
        sharer = User(username="aaliya", email="aaliya@example.com")
        listener = User(username="kenji", email="kenji@example.com")
        outsider = User(username="simone", email="simone@example.com")
        db.session.add_all([sharer, listener, outsider])
        db.session.flush()
        db.session.execute(friendships.insert().values(user_id=sharer.id, friend_id=listener.id))

        anthem = Song(
            title="Crown Heights Anthem",
            artist="Borough Kings",
            shared_by=sharer.id,
            tags=[Tag(name="rap"), Tag(name="hip-hop"), Tag(name="boom bap")],
        )
        other = Song(
            title="Midnight Drive",
            artist="The Wanderers",
            shared_by=sharer.id,
        )
        playlist = Playlist(name="Friday Energy", created_by=listener.id)
        db.session.add_all([anthem, other, playlist])
        db.session.commit()
        yield app.test_client(), sharer, listener, outsider, anthem, other, playlist
        db.session.remove()
        db.drop_all()


def test_streak_crosses_sunday_and_resets_after_gap(sample):
    client, _, listener, _, anthem, _, _ = sample
    saturday = datetime(2026, 11, 14, 23, tzinfo=timezone.utc)
    listener.last_listened_at = saturday
    listener.listening_streak = 12
    db.session.commit()
    listen_url = f"/songs/{anthem.id}/listen"
    streak_url = f"/users/{listener.id}/streak"

    for days, expected in [(1, 13), (1, 13), (2, 14), (4, 1)]:
        with patch("services.streak_service.datetime") as clock:
            clock.now.return_value = saturday + timedelta(days=days, hours=-12)
            assert client.post(listen_url, json={"user_id": listener.id}).status_code == 201
        assert client.get(streak_url).get_json()["streak"] == expected


def test_listening_now_starts_at_utc_midnight(sample):
    client, sharer, listener, outsider, anthem, other, _ = sample
    midnight = datetime(2026, 11, 15, tzinfo=timezone.utc)
    db.session.add_all([
        ListeningEvent(user_id=listener.id, song_id=anthem.id,
                       listened_at=midnight - timedelta(hours=1)),
        ListeningEvent(user_id=outsider.id, song_id=other.id, listened_at=midnight),
    ])
    db.session.commit()
    listening_now = f"/feed/{sharer.id}/listening-now"

    with patch("services.feed_service.datetime") as clock:
        clock.now.return_value = midnight + timedelta(hours=9)
        assert client.get(listening_now).get_json()["feed"] == []
        db.session.add_all([
            ListeningEvent(user_id=listener.id, song_id=anthem.id, listened_at=midnight),
            ListeningEvent(user_id=listener.id, song_id=other.id,
                           listened_at=midnight + timedelta(hours=1)),
        ])
        db.session.commit()
        current = client.get(listening_now).get_json()["feed"]
        assert [event["song"]["id"] for event in current] == [other.id]

    activity = client.get(f"/feed/{sharer.id}/activity").get_json()["feed"]
    assert [item["song"]["id"] for item in activity] == [other.id, anthem.id, anthem.id]


def test_search_returns_one_song_with_all_its_tags(sample):
    client, _, _, _, anthem, other, _ = sample
    matches = client.get("/songs/search?q=aNtHeM").get_json()
    assert [song["id"] for song in matches["results"]] == [anthem.id]
    assert set(matches["results"][0]["tags"]) == {"rap", "hip-hop", "boom bap"}
    artist = client.get("/songs/search?q=wanderers").get_json()
    assert [song["id"] for song in artist["results"]] == [other.id]


def test_rating_notifies_sharer_without_duplicate_or_self_notices(sample):
    client, sharer, listener, _, anthem, other, _ = sample
    endpoint = f"/songs/{anthem.id}/rate"
    feed = f"/users/{sharer.id}/notifications?unread_only=true"

    response = client.post(endpoint, json={"user_id": listener.id, "score": 5})
    assert response.status_code == 201
    rating = response.get_json()
    notices = client.get(feed).get_json()["notifications"]
    assert [notice["type"] for notice in notices] == ["song_rated"]
    assert "kenji" in notices[0]["body"]
    assert "Crown Heights Anthem" in notices[0]["body"]
    assert "5" in notices[0]["body"]

    assert client.post(endpoint, json={"user_id": listener.id, "score": 5}).status_code == 201
    assert client.post(endpoint, json={"user_id": listener.id, "score": 6}).status_code == 400
    assert client.post(f"/songs/{other.id}/rate", json={"user_id": sharer.id, "score": 3}).status_code == 201
    assert client.get(feed).get_json()["count"] == 1
    updated = client.post(endpoint, json={"user_id": listener.id, "score": 4}).get_json()
    assert (updated["id"], updated["score"]) == (rating["id"], 4)
    assert client.get(feed).get_json()["count"] == 2


def test_playlist_list_returns_the_only_stored_song(sample):
    client, _, listener, _, anthem, _, playlist = sample
    db.session.execute(playlist_entries.insert().values(
        playlist_id=playlist.id, song_id=anthem.id, position=1, added_by=listener.id,
    ))
    db.session.commit()
    result = client.get(f"/playlists/{playlist.id}/songs").get_json()
    assert [song["id"] for song in result["songs"]] == [anthem.id]


def test_playlist_append_returns_all_songs_in_order_and_notifies_sharer(sample):
    client, sharer, listener, _, anthem, other, playlist = sample
    endpoint = f"/playlists/{playlist.id}/songs"
    assert client.get(endpoint).get_json()["count"] == 0

    for song in (anthem, other, other):
        assert client.post(endpoint, json={"song_id": song.id, "added_by": listener.id}).status_code == 201

    result = client.get(endpoint).get_json()
    assert [song["id"] for song in result["songs"]] == [anthem.id, other.id]
    assert result["count"] == 2
    notices = client.get(f"/users/{sharer.id}/notifications").get_json()["notifications"]
    assert [notice["type"] for notice in notices] == ["song_added_to_playlist"] * 2
