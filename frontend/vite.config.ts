import {defineConfig} from "vite";
import react from "@vitejs/plugin-react-swc";
import {createHtmlPlugin} from "vite-plugin-html";
import {execSync} from "child_process";
import {readFileSync} from "fs";
import {resolve} from "path";

const backendTarget = process.env.BACKEND_URL ?? "http://127.0.0.1:8000";
const packageJson = JSON.parse(readFileSync(resolve(__dirname, "package.json"), "utf-8"));

function gitSha(): string {
    try {
        return execSync("git rev-parse --short HEAD", {cwd: __dirname}).toString().trim();
    } catch {
        return "unknown";
    }
}

const buildInfo = {
    version: packageJson.version as string,
    sha: process.env.CF_PAGES_COMMIT_SHA?.slice(0, 7) ?? process.env.GITHUB_SHA?.slice(0, 7) ?? gitSha(),
    builtAt: new Date().toISOString(),
};

// https://vitejs.dev/config/
export default defineConfig({
    define: {
        __BUILD_INFO__: JSON.stringify(buildInfo),
    },
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
