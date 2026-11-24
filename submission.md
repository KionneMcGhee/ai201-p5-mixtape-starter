# Project 5 — Mixtape Bug Hunt

## AI usage

I gave ChatGPT the project brief and asked it to complete the bug hunt. It helped trace how requests move from Flask routes to services and database models. For search, it followed the title filter into the tag association and used a three-tag song plus an untagged search match to verify how the query and model relationship each contributed to the response.

For playlists, ChatGPT traced the read and write endpoints separately. The initial read diagnosis accounted for the missing last song. Running the add-song request against the starter revealed a second failure, a database constraint error, which required a change to the insert path as well. ChatGPT implemented the fixes, ran the tests on the starter and fixed code, and drafted this write-up. The investigation and verification steps below describe those AI-assisted runs.

## Codebase map

| File | Responsibility |
| --- | --- |
| `app.py` | Builds the Flask app, configures SQLAlchemy, registers four route blueprints, and creates the database tables. The default database is SQLite. |
| `models.py` | Defines `User`, `Tag`, `Song`, `ListeningEvent`, `Rating`, `Playlist`, and `Notification`. The `friendships`, `song_tags`, and `playlist_entries` tables link records. Playlist entries also store position, contributor, and time added. |
| `routes/songs.py` | Handles song search, song details, ratings, and play submissions. It reads query strings and JSON, then formats service results and errors as JSON responses. |
| `routes/playlists.py` | Handles playlist creation, details, ordered song listings, and song additions. |
| `routes/users.py` | Returns user profiles, current streaks, and notifications, and accepts requests to mark notifications as read. |
| `routes/feed.py` | Serves the listening-now and recent friend activity feeds. |
| `services/streak_service.py` | Records listening events and maintains the stored streak and last-listen timestamp on the user. |
| `services/feed_service.py` | Traverses friendship relationships and assembles listening and activity feeds from event and song data. |
| `services/search_service.py` | Searches the catalog by title or artist and serializes song details with tags. |
| `services/notification_service.py` | Handles rating and playlist-add writes and manages creation, retrieval, and read state for notifications. |
| `services/playlist_service.py` | Creates playlists and retrieves playlist metadata and song entries. |
| `seed_data.py` | Builds a sample database with users, friendships, shared songs, listening history, and playlists. |
| `tests/` | Exercises search, streaks, and playlist retrieval with isolated SQLite fixtures. |

**Data flow for a playlist addition.** `POST /playlists/<playlist_id>/songs` is handled by `add_song()` in `routes/playlists.py`. The route reads `song_id` and `added_by` from the request and calls `add_to_playlist()` in `services/notification_service.py`. The service looks up the playlist, song, and contributor and writes to the playlist-song association. The association stores both the contributor and the song's position. When a different user shared the song, the service also creates a notification for the sharer. A separate `GET` for the playlist songs calls `get_playlist_songs()` to join those entries back to `Song` and return the ordered list.

The routes define the HTTP interface and mostly delegate application rules to services. Services share the SQLAlchemy session through `app.db`. Model `to_dict()` methods supply common response fields, and services can attach feature-specific values before the routes serialize them.

## Root cause analysis

### Issue #1 — My listening streak keeps resetting

1. **Reproduction.** In a test database, Kenji's streak was 12 and his last listen was Saturday at 23:00 UTC. With the app clock set to Sunday at 11:00 UTC, the test submitted a listen through `POST /songs/<song_id>/listen` and checked `GET /users/<user_id>/streak`. The starter returned 1. The expected result was 13.
2. **Navigation strategy.** The streak GET route in `routes/users.py` delegates to `get_streak()`, which returns the saved value. That pointed to the write path. The investigation followed the song listen route in `routes/songs.py` to `record_listening_event()` and then `update_listening_streak()` in `services/streak_service.py`. For the failing timestamps, `days_since_last` was 1 and `today.weekday()` was 6, so the increment condition evaluated to false.
3. **The root cause.** The increment branch required both a one-day gap and a weekday value other than 6. Python assigns 6 to Sunday, so a valid Saturday-to-Sunday pair fell through to the same reset branch as a skipped day.
4. **Fix.** Base the increment on `days_since_last == 1`. Consecutive calendar dates now follow the same rule every day of the week.
5. **Side-effect check.** The endpoint test checked a second Sunday play, a Monday play, and a Wednesday play after skipping Tuesday. The streak stayed 13 on the same day, rose to 14 on Monday, and reset to 1 after the gap. The starter's first-listen test also passed.

### Issue #2 — Friends Listening Now shows people from yesterday

1. **Reproduction.** The test set the clock to 09:00 UTC and gave a friend one event at 23:00 UTC the previous day. `GET /feed/<user_id>/listening-now` showed the friend's previous-night track. The fixture also included a current-day play by someone outside the viewer's friend list.
2. **Navigation strategy.** The investigation followed `listening_now()` in `routes/feed.py` into `get_friends_listening_now()` in `services/feed_service.py`. The friend's ID was correctly in the friendship filter, and the badge reflected the latest event returned by the SQL query. At 09:00 the cutoff calculated by subtracting 24 hours was yesterday at 09:00, which admitted the 23:00 event before the result loop ran.
3. **The root cause.** The query used a rolling 24-hour window. The elapsed time between 23:00 and 09:00 is only 10 hours, even though the timestamps are on different calendar dates.
4. **Fix.** Filter timestamps from today's UTC midnight, inclusive, to tomorrow's UTC midnight, exclusive. UTC matches the clock used when recording listening events.
5. **Side-effect check.** The checks confirmed that current-day plays appear, the latest play wins when a friend plays twice, and an outsider's play stays out. The separate activity endpoint still returned the previous-night friend event alongside today's plays because it intentionally lists recent history across dates.

### Issue #3 — The same song keeps showing up twice in search

1. **Reproduction.** The fixture gave Crown Heights Anthem the tags `rap`, `hip-hop`, and `boom bap`, then the test called `GET /songs/search?q=aNtHeM`. The response contained the same song ID three times.
2. **Navigation strategy.** The search route in `routes/songs.py` passes the query to `search_songs()` and reports the length of its returned list. The service query in `services/search_service.py` joins `Song` to `song_tags` before returning scalars. Comparing the repeated ID in the response with the three tag associations showed where the extra rows came from. `Song.tags` in `models.py` already loads the tags included by `Song.to_dict()`.
3. **The root cause.** The outer join returns one SQL row per matching tag association. With three associations, `.scalars().all()` yielded the same song three times and serialization repeated it three times. The title and artist filters only use columns from `Song`.
4. **Fix.** Remove the association join from the search query. Selecting from `Song` returns one row per matching song ID, and its existing relationship supplies all tags to the serializer.
5. **Side-effect check.** The mixed-case title search returned one result with all three original tags. An artist search still found the untagged song. The starter tests also passed for single-tag, no-tag, and no-match cases.

### Issue #4 — A friend rated my song and I got no notification

1. **Reproduction.** Kenji rated Aaliya's song 5 through `POST /songs/<song_id>/rate`. The endpoint returned HTTP 201 and a saved rating. `GET /users/<aaliya_id>/notifications?unread_only=true` returned an empty list.
2. **Navigation strategy.** The investigation followed the rating POST into `rate_song()` in `services/notification_service.py` and followed the user's inbox endpoint back to `get_notifications()`. The inbox reads stored notices by recipient. Comparing `rate_song()` with `add_to_playlist()` in the same service showed that the playlist path calls `create_notification()` for cross-user actions, while the rating path only saved the rating.
3. **The root cause.** A `Rating` row stores the score. The inbox loads `Notification` rows, so saving a rating alone leaves nothing for the inbox to display.
4. **Fix.** After creating or changing another person's rating on a shared song, call `create_notification()` with the sharer's ID, the `song_rated` type, and a message with the rater, title, and score. That helper commits the rating and the new notice together. Self-ratings save normally, and repeating the same score leaves the existing rating in place.
5. **Side-effect check.** A new score created a notice for the sharer. Changing 5 to 4 reused the original rating ID and created a second notice. Submitting 5 again before the change, rating one's own song, and submitting an invalid score produced no additional notices. The playlist regression test separately verifies that its original notification type still arrives.

### Issue #5 — The last song in a playlist never shows up

1. **Reproduction.** The test queried a playlist with one stored entry through `GET /playlists/<playlist_id>/songs` and received an empty list. With the starter's five-entry fixture, the service returned four. The second test attempted to add a song with `POST /playlists/<playlist_id>/songs`, which raised SQLite's `NOT NULL` error for `playlist_entries.position`.
2. **Navigation strategy.** The investigation followed `get_songs()` in `routes/playlists.py` to `get_playlist_songs()` in `services/playlist_service.py`. The query joined the song and association tables and ordered by position. The final slice discarded the last item after the query. For the add failure, The investigation followed `add_song()` to `add_to_playlist()` in `services/notification_service.py` and compared the failed INSERT columns with the required fields in `models.py`.
3. **The root cause.** The `songs[:-1]` slice always excludes the last result, which is the newest entry when sorted by position. The add path had a separate problem. Appending directly to `playlist.songs` supplied the foreign keys but omitted the required `position` and `added_by` values.
4. **Fix.** Serialize the complete ordered query result. Insert each new association with its contributor and next position, using 1 for an empty playlist and the maximum existing position plus 1 otherwise. A retry of an existing song keeps the same row and skips an extra notice. Successful additions by someone other than the sharer still notify the sharer in the same transaction.
5. **Side-effect check.** The tests covered empty, one-song, and multi-song playlists. Adding two songs through the HTTP endpoint made both appear in insertion order. Retrying the second add left two songs and two total playlist notices. The starter's five-song test still returned tracks in the original order.

## Regression tests

Run `python -m pytest tests/ -q` after installing the dependencies. The full suite finished with **19 passed**.

The six added endpoint tests are in [tests/test_bug_hunt_regressions.py](tests/test_bug_hunt_regressions.py). Each test failed against the starter and passed after the fixes. For example, `test_listening_now_starts_at_utc_midnight` fixes the clock at 09:00 UTC and creates a friend event at 23:00 UTC the prior day. The starter returned that event because it was younger than 24 hours. The fixed endpoint excludes it and still includes the most recent current-day friend event.
