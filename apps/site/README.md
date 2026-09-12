# The public site

Three static pages at **<https://clipforge.bytepic.dev>**: the landing page, the
privacy policy and the terms. No build step, no framework, no JavaScript — what
is in this folder is what is served.

The Firebase Hosting site is still named `getclipforge`, so the same pages also
answer on `getclipforge.web.app`. That is an alias, not a second site: every
canonical tag, `og:url` and sitemap entry names the custom domain, because
`web.app` is Google's registrable domain rather than ours — which is also why
the OAuth consent screen would not verify against it.

## Why it exists at all

Google's OAuth consent screen will not accept an application without a home page
and a privacy policy on an authorised domain, and the YouTube API Services
Developer Policies require the privacy policy to say certain things by name.
Those pages had to exist somewhere public, and once they did, a landing page in
front of them cost almost nothing.

## Why it is a second Hosting site

`bytepic-clipforge.web.app` is the PWA, and its `**` rewrite sends every path to
the Angular app — so `/privacy` there is the app, not a policy. Adding a second
Firebase Hosting site in the same project keeps the app's routing untouched and
gives the public pages a root of their own, which is also where an SEO-worthy
landing page belongs.

## Deploying

```powershell
powershell -File tools/deploy.ps1 -Only site
```

That resolves to `firebase deploy --project <id> --only hosting:getclipforge`.
Never a bare `--only hosting`: `firebase.json` now holds two sites, and that
would also publish whatever is sitting in `apps/web/dist` — possibly a build
still pointed at the local emulators.

## Changing the pages

- **Content and structure** live in the three HTML files. Each is standalone;
  the header, footer and metadata are repeated rather than templated, because
  three copies of thirty lines is cheaper than a build step.
- **Styling** is `assets/site.css`, shared by all three.
- **Images** in `img/` are generated — do not edit them by hand. Run
  `python tools/brand-assets.py` from the repository root, which redraws them
  from `logo.png` along with the YouTube channel art.
- **A new page** needs an entry in `sitemap.xml` and a link from somewhere, or
  nothing will find it.

If you change a URL, change it in `sitemap.xml`, in the page's own `<link
rel="canonical">` and `og:url`, and in the Google Cloud console under
**OAuth consent screen → Branding**, where the home page and privacy policy
links are recorded and validated against the authorised domains.

## Content Security Policy

The site is served under a strict CSP set in `firebase.json`: `default-src
'self'`, no inline styles, no third-party anything. Adding a webfont, an
analytics tag or an embedded video means changing that header — which is the
point of it being there.
