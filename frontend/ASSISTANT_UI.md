# Shared assistant UI

The assistant implementation lives in the public repository
https://github.com/jcardus/traccar-assistant-ui (local checkout:
~/traccar-assistant-ui). Both traccar-dash and traccar-chatkit/frontend install
@traccar/assistant-ui from Git, pinned to a commit in their package.json and
package-lock.json files. Nothing is published to npm.

The local ChatKitPanel is an adapter for API settings, dashboard theme, and
layout. Change tool handling, attachments, screenshots, history restoration,
or printing in the shared repository. The dashboard has no show_map renderer;
it reports that tool as unsupported. The standalone app supplies its map renderer.

## Update both frontends

1. Edit and build the shared package (`npm ci`, then `npm run build`).
2. Commit and push the shared repository.
3. In each consumer, run `npm install git+https://github.com/jcardus/traccar-assistant-ui.git#FULL_COMMIT_SHA`.
4. Build both frontends and commit each consumer's package.json and lockfile.

## Deployment

The public Git dependency resolves over HTTPS, so builds do not need GitHub
credentials. Keep HTTPS in the lockfile when updating the package. npm's Git
dependency prepare step installs build dependencies and runs TypeScript.

Both local production builds passed. App layout and backend endpoint/proxy
configuration stay in the consumer repositories.
