import {defineConfig} from "vite";
import react from "@vitejs/plugin-react-swc";
import {createHtmlPlugin} from "vite-plugin-html";
import {readFileSync} from "fs";
import {resolve} from "path";

const backendTarget = process.env.BACKEND_URL ?? "http://127.0.0.1:8000";
const packageJson = JSON.parse(readFileSync(resolve(__dirname, "package.json"), "utf-8"));

// https://vitejs.dev/config/
export default defineConfig({
    plugins: [
        react(),
        createHtmlPlugin({
            inject: {
                data: {
                    appVersion: packageJson.version,
                },
            },
        }),
    ],
    server: {
        port: 5170,
        host: "0.0.0.0",
        proxy: {
            "/api": {
                target: "http://gps.frotaweb.com",
            },
            "/chatkit": {
                target: backendTarget,
                changeOrigin: true,
            },
            "/facts": {
                target: backendTarget,
                changeOrigin: true,
            },
        },
        // For production deployments, you need to add your public domains to this list
        allowedHosts: [
            // You can remove these examples added just to demonstrate how to configure the allowlist
            ".ngrok.io",
            ".trycloudflare.com",
        ],
    },
    base: "/chat/",
});
