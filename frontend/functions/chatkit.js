export const onRequest = ({request, env}, cf) => {
    const url = new URL(request.url)
    url.host = env.CHAT_SERVER || 'localhost'
    url.protocol = 'http:'
    url.port = 8000
    return fetch(new Request(url, request), cf)
}
