/**
 * The one thing the Track field refuses, and what to offer instead.
 *
 * A link copied out of the YouTube app's Share button carries the mix that was
 * playing — `?list=RD…` on the end — and yt-dlp reads that as an instruction to
 * fetch the whole thing. One MUSIC job ran for half an hour and pulled 190
 * tracks, 905 MB, before anyone looked at it.
 *
 * The worker refuses these links too, and that is the guard that counts: a job
 * can reach it without passing through this screen. This is the same rule
 * stated in the browser, before a job exists. The worker's answer only arrives
 * once a job has been created, queued, claimed and failed, and on this project
 * a job that sits at QUEUED means going to find out whether a worker is even
 * running — a long way round to a fact the URL already contains.
 *
 * Refused rather than quietly reduced to the single video it contains. `list=`
 * can mean "play me this mix" or "here is the track I meant, with some rubbish
 * on the end", and choosing between those on the user's behalf is a guess. The
 * suggestion handed back with the refusal is what makes refusing worth more
 * than guessing: it says what is wrong *and* gives the link that works, so the
 * cost is one more paste rather than a trip to YouTube and back.
 */

/**
 * Mirrors `_YOUTUBE_HOSTS` in apps/worker/clipforge/media/sources.py. A `list=`
 * on any other host is somebody else's query parameter and none of our
 * business.
 */
const YOUTUBE_HOSTS = new Set([
  'youtube.com',
  'www.youtube.com',
  'm.youtube.com',
  'music.youtube.com',
  'youtu.be',
  'www.youtu.be',
]);

/** YouTube's video ids: eleven characters of base64url, always. */
const VIDEO_ID = /^[A-Za-z0-9_-]{11}$/;

/** The path shapes that carry a video id, in the worker's order. */
const ID_PATH_PREFIXES = ['/shorts/', '/embed/', '/v/', '/live/'];

export interface YoutubePlaylistLink {
  /**
   * The `list=` value as the link gave it — `RDD2XUoPg3-KY`, `PL…`, `WL`.
   *
   * Reported so the message can point at the part of a long URL that is the
   * problem. Deliberately not interpreted: a mix, a real playlist, Watch Later
   * and Liked all send yt-dlp down the same road, so there is nothing to gain
   * by telling them apart and a prefix nobody has seen yet to get wrong.
   */
  readonly listId: string;
  /**
   * The single-video URL to use instead, when the link names a video at all.
   *
   * `null` for `/playlist?list=…`, which names a collection and no video — so
   * there is nothing in it to suggest, and the message says so rather than
   * inventing one.
   */
  readonly singleVideoUrl: string | null;
}

/**
 * Whether what the user typed names a YouTube playlist, and what to suggest.
 *
 * `null` means "nothing wrong with this as far as this rule goes" — a bare
 * video id, an ordinary watch link, and a path to a file on the worker all come
 * back null. That last one matters: the field takes a local audio path as
 * readily as a link, so anything that is not recognisably a YouTube URL has to
 * pass. It is not this function's job to decide whether a file exists.
 */
export function youtubePlaylist(submission: string): YoutubePlaylistLink | null {
  const url = asUrl(submission.trim());
  if (!url || !YOUTUBE_HOSTS.has(url.hostname.toLowerCase())) return null;

  // A `list=` that carries no id is not a playlist. YouTube itself emits
  // `&list=` empty on some share paths, and refusing that would refuse a link
  // that plays one video and nothing else.
  const listId = url.searchParams.get('list')?.trim() ?? '';
  if (!listId) return null;

  const videoId = videoIdIn(url);
  return {
    listId,
    singleVideoUrl: videoId ? `https://www.youtube.com/watch?v=${videoId}` : null,
  };
}

/**
 * Parse loosely, the way the worker's `parse_youtube_id` does, so that a
 * `youtu.be/ID?list=X` pasted without its scheme is still recognised.
 *
 * Supplying the missing scheme is safe beside a field that also takes file
 * paths, because a path can only ever come out of this with a host that is not
 * YouTube's: `C:\Music\bed.m4a` parses with the host `c`, `/srv/music/bed.m4a`
 * with the host `srv`. Both fail the host check a line later, which is exactly
 * what a local file needs to happen. `null` is for the few strings the parser
 * will not take at all, and they are let through for the same reason.
 */
function asUrl(candidate: string): URL | null {
  if (!candidate) return null;
  try {
    return new URL(candidate.includes('//') ? candidate : `https://${candidate}`);
  } catch {
    return null;
  }
}

/** The video id in a YouTube URL, by the same paths `parse_youtube_id` reads. */
function videoIdIn(url: URL): string | null {
  if (url.hostname.toLowerCase().endsWith('youtu.be')) {
    return validId(url.pathname.replace(/^\/+/, '').split('/')[0]);
  }
  if (url.pathname === '/watch') {
    return validId(url.searchParams.get('v'));
  }
  for (const prefix of ID_PATH_PREFIXES) {
    if (url.pathname.startsWith(prefix)) {
      return validId(url.pathname.slice(prefix.length).split('/')[0]);
    }
  }
  return null;
}

function validId(value: string | null | undefined): string | null {
  return value && VIDEO_ID.test(value) ? value : null;
}
