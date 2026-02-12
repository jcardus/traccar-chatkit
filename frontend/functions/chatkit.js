export const onRequest = ({request, env}, cf) => {
    const url = new URL(request.url)
    url.host = env.CHAT_SERVER || 'localhost'
    console.log(url)
    return fetch(new Request(url, request), cf)
}
