import { describe, expect, it } from 'vitest';

import { youtubePlaylist } from './music-source';

/**
 * The Track field's playlist guard.
 *
 * The case that pays for all of this is the first one: that exact link, copied
 * out of the YouTube app's Share button, cost half an hour and 905 MB of
 * downloaded mix. The rest is the boundary around it — everything the field is
 * meant to keep accepting, which is most of what anyone types into it, and a
 * local audio path above all.
 */

/** The link from the incident, and the one video that was actually wanted. */
const MIX_LINK = 'https://youtu.be/KNdOc9vbESI?list=RDD2XUoPg3-KY';
const THE_ONE_VIDEO = 'https://www.youtube.com/watch?v=KNdOc9vbESI';

describe('youtubePlaylist', () => {
  it('names the mix a shared YouTube link drags along with it', () => {
    expect(youtubePlaylist(MIX_LINK)).toEqual({
      listId: 'RDD2XUoPg3-KY',
      singleVideoUrl: THE_ONE_VIDEO,
    });
  });

  it('hands back the single-video URL rather than leaving the user to build it', () => {
    // Refusing is only better than guessing if the refusal is more useful than
    // the guess would have been. Without this the user goes to YouTube, finds
    // the track again and copies a second link; with it they paste one line.
    expect(
      youtubePlaylist('https://www.youtube.com/watch?v=KNdOc9vbESI&list=PLabc123')?.singleVideoUrl,
    ).toBe(THE_ONE_VIDEO);
    expect(
      youtubePlaylist('https://m.youtube.com/watch?v=KNdOc9vbESI&list=RDAMVMabc')?.singleVideoUrl,
    ).toBe(THE_ONE_VIDEO);
    expect(
      youtubePlaylist('https://www.youtube.com/shorts/KNdOc9vbESI?list=WL')?.singleVideoUrl,
    ).toBe(THE_ONE_VIDEO);
  });

  it('recognises a link pasted without its scheme, which is how people paste them', () => {
    expect(youtubePlaylist('youtu.be/KNdOc9vbESI?list=RDD2XUoPg3-KY')?.singleVideoUrl).toBe(
      THE_ONE_VIDEO,
    );
    expect(youtubePlaylist('www.youtube.com/watch?v=KNdOc9vbESI&list=PLabc')?.singleVideoUrl).toBe(
      THE_ONE_VIDEO,
    );
  });

  it('refuses every kind of list, without asking what kind it is', () => {
    // Mixes, real playlists, Watch Later and Liked all send yt-dlp down the
    // same road. Telling them apart by prefix would buy nothing and would get
    // the next prefix YouTube invents wrong.
    for (const list of ['RDD2XUoPg3-KY', 'PLxLqEVBJpSj', 'WL', 'LL', 'OLAK5uy_nSomething']) {
      expect(youtubePlaylist(`https://www.youtube.com/watch?v=KNdOc9vbESI&list=${list}`)).toEqual({
        listId: list,
        singleVideoUrl: THE_ONE_VIDEO,
      });
    }
  });

  it('suggests nothing for a playlist URL that names no video, because there is none', () => {
    // The message has to change shape for this one: /playlist?list= is a
    // collection and nothing else, so there is no single video in it to offer
    // and inventing one would mean picking whichever happened to be first.
    expect(youtubePlaylist('https://www.youtube.com/playlist?list=PLxLqEVBJpSj')).toEqual({
      listId: 'PLxLqEVBJpSj',
      singleVideoUrl: null,
    });
  });

  it('suggests nothing when the video id in the link is not a video id', () => {
    expect(
      youtubePlaylist('https://www.youtube.com/watch?v=tooshort&list=PLabc')?.singleVideoUrl,
    ).toBeNull();
  });

  it('lets an ordinary single-video link through untouched', () => {
    expect(youtubePlaylist(THE_ONE_VIDEO)).toBeNull();
    expect(youtubePlaylist('https://youtu.be/KNdOc9vbESI')).toBeNull();
    expect(youtubePlaylist('https://www.youtube.com/watch?v=KNdOc9vbESI&t=42s')).toBeNull();
    expect(youtubePlaylist('https://www.youtube.com/shorts/KNdOc9vbESI')).toBeNull();
  });

  it('lets a bare video id through', () => {
    expect(youtubePlaylist('KNdOc9vbESI')).toBeNull();
    expect(youtubePlaylist('dQw4w9Wg_cQ')).toBeNull();
  });

  it('treats an empty or blank list as no list at all', () => {
    // YouTube emits a bare `&list=` on some share paths. Refusing that would
    // refuse a link that plays one video and nothing else.
    expect(youtubePlaylist('https://www.youtube.com/watch?v=KNdOc9vbESI&list=')).toBeNull();
    expect(youtubePlaylist('https://www.youtube.com/watch?v=KNdOc9vbESI&list=%20%20')).toBeNull();
  });

  it('leaves a path to a file on the worker alone, Windows drive letters included', () => {
    // The field takes "a path to an audio file on the worker" as readily as a
    // link, and a guard that flagged one would make the other half of the field
    // unusable. Note the second: a file whose own name contains `list=` is
    // still a file.
    expect(youtubePlaylist(String.raw`C:\Music\Backing tracks\bed.m4a`)).toBeNull();
    expect(youtubePlaylist(String.raw`D:\mixes\list=RDD2XUoPg3-KY.mp3`)).toBeNull();
    expect(youtubePlaylist(String.raw`\nas\music\bed.m4a`)).toBeNull();
    expect(youtubePlaylist('/srv/clipforge/music/bed.m4a')).toBeNull();
    expect(youtubePlaylist('./bed.m4a')).toBeNull();
    expect(youtubePlaylist('file:///C:/Music/bed.m4a')).toBeNull();
  });

  it('does not read a list= on another site as a YouTube playlist', () => {
    // `list` is an ordinary query parameter. Only YouTube reads it as an
    // instruction to download a hundred and ninety things.
    expect(youtubePlaylist('https://example.com/tracks?list=RDD2XUoPg3-KY')).toBeNull();
    expect(youtubePlaylist('https://notyoutube.com/watch?v=KNdOc9vbESI&list=PLabc')).toBeNull();
    expect(youtubePlaylist('https://youtube.com.evil.test/watch?v=abc&list=PLabc')).toBeNull();
  });

  it('says nothing about an empty field', () => {
    expect(youtubePlaylist('')).toBeNull();
    expect(youtubePlaylist('   ')).toBeNull();
  });
});
