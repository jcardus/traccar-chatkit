import { Map, Source, Layer } from "react-map-gl/maplibre";
import "maplibre-gl/dist/maplibre-gl.css";
import {useEffect, useRef} from "react";
import bbox from "@turf/bbox";

export default function GeoJsonMapGL({ data }) {
    const mapRef = useRef(null);
    useEffect(() => {
        if (!mapRef.current || !data) return;
        mapRef.current.fitBounds(bbox(data), { padding: 40, duration: 1000 });
    }, [data]);

    return (
        <Map
            ref={mapRef}
            initialViewState={{ longitude: 0, latitude: 0, zoom: 2 }}
            style={{ width: "100%", height: "100%" }}
            mapStyle="https://basemaps.cartocdn.com/gl/positron-gl-style/style.json"
        >
            <Source id="geojson" type="geojson" data={data}>
                <Layer
                    id="geojson-fill"
                    type="fill"
                    paint={{
                        "fill-color": "#088",
                        "fill-opacity": 0.5,
                    }}
                />
            </Source>
        </Map>
    );
}
