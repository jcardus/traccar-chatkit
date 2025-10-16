import { Map, Source, Layer } from "react-map-gl/maplibre";
import "maplibre-gl/dist/maplibre-gl.css";
import {useEffect, useMemo, useRef} from "react";
import bbox from "@turf/bbox";

export default function GeoJsonMapGL({ data }) {
    const mapRef = useRef(null);
    useEffect(() => {
        if (!mapRef.current || !data) return;
        setTimeout(() => {mapRef.current.fitBounds(bbox(data), { padding: 40, duration: 1000 })}, 1000)
    }, [data]);

    const layers = useMemo(() => {
        if (!data?.features) return [];

        // Extract distinct styles by geometry type
        const defaultStyles = {
            Point: { circleColor: "#ff0000", circleRadius: 6 },
            LineString: { lineColor: "#2196f3", lineWidth: 3 },
            Polygon: { fillColor: "#0080ff", fillOpacity: 0.3, outlineColor: "#0080ff" },
        };
        // Layers — can be extended for per-feature overrides later
        return [
            // Polygons (fill)
            {
                id: "geojson-fill",
                type: "fill",
                filter: ["==", "$type", "Polygon"],
                paint: {
                    "fill-color": [
                        "coalesce",
                        ["get", "fillColor", ["get", "style"]],
                        defaultStyles.Polygon.fillColor,
                    ],
                    "fill-opacity": [
                        "coalesce",
                        ["get", "fillOpacity", ["get", "style"]],
                        defaultStyles.Polygon.fillOpacity,
                    ],
                },
            },
            // Polygon outlines
            {
                id: "geojson-outline",
                type: "line",
                filter: ["==", "$type", "Polygon"],
                paint: {
                    "line-color": [
                        "coalesce",
                        ["get", "strokeColor", ["get", "style"]],
                        defaultStyles.Polygon.outlineColor,
                    ],
                    "line-width": 2,
                },
            },
            // Lines
            {
                id: "geojson-line",
                type: "line",
                filter: ["==", "$type", "LineString"],
                paint: {
                    "line-color": [
                        "coalesce",
                        ["get", "lineColor", ["get", "style"]],
                        defaultStyles.LineString.lineColor,
                    ],
                    "line-width": [
                        "coalesce",
                        ["get", "lineWidth", ["get", "style"]],
                        defaultStyles.LineString.lineWidth,
                    ],
                },
            },
            // Points
            {
                id: "geojson-point",
                type: "circle",
                filter: ["==", "$type", "Point"],
                paint: {
                    "circle-radius": [
                        "coalesce",
                        ["get", "pointRadius", ["get", "style"]],
                        defaultStyles.Point.circleRadius,
                    ],
                    "circle-color": [
                        "coalesce",
                        ["get", "pointColor", ["get", "style"]],
                        defaultStyles.Point.circleColor,
                    ],
                    "circle-stroke-color": "#fff",
                    "circle-stroke-width": 1.5,
                },
            },
            // Labels
            {
                id: "geojson-label",
                type: "symbol",
                layout: {
                    "text-field": [
                        "coalesce",
                        ["get", "name"],
                        ["get", "title"],
                        ["get", "label"],
                        "",
                    ],
                    "text-size": 12,
                    "text-offset": [0, 1.5],
                    "text-anchor": "top",
                },
                paint: {
                    "text-color": "#000000",
                    "text-halo-color": "#ffffff",
                    "text-halo-width": 2,
                },
            },
        ];
    }, [data]);

    return data && (
        <Map
            ref={mapRef}
            initialViewState={{ longitude: 0, latitude: 0, zoom: 2 }}
            style={{ width: "100%", height: "100%" }}
            mapStyle="https://basemaps.cartocdn.com/gl/positron-gl-style/style.json"
        >
            <Source id="geojson" type="geojson" data={data}>
                {layers.map((layer) => (
                    // @ts-ignore
                    <Layer key={layer.id} {...layer} />
                ))}
            </Source>
        </Map>
    );
}
