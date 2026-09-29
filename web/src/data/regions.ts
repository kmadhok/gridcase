// Coarse balancing-authority footprints for the map (approximate outlines, not legal
// boundaries). Lon/lat pairs. ERCOT covers most of Texas but not the panhandle or El Paso;
// CAISO most of California; PJM the mid-Atlantic and Ohio valley.
export const REGION_POLYGONS: Record<string, [number, number][]> = {
  ERCO: [
    [-103.2, 31.9], [-101.8, 33.6], [-100.0, 34.6], [-97.2, 33.9], [-94.6, 33.5], [-94.0, 31.9],
    [-93.9, 30.0], [-96.4, 28.5], [-97.4, 26.6], [-99.0, 26.4], [-101.4, 29.6], [-103.5, 30.6],
  ],
  CISO: [
    [-124.4, 42.0], [-120.0, 42.0], [-120.0, 39.0], [-118.5, 37.2], [-114.6, 35.0], [-114.6, 32.7],
    [-117.1, 32.5], [-119.5, 34.3], [-122.5, 37.5], [-124.4, 40.4],
  ],
  PJM: [
    [-90.6, 42.5], [-87.4, 42.5], [-84.8, 41.7], [-80.5, 42.3], [-76.0, 42.0], [-73.9, 40.6],
    [-75.0, 38.0], [-76.0, 36.6], [-79.0, 36.5], [-84.0, 37.8], [-88.6, 37.9], [-90.6, 39.2],
  ],
};

export interface RegionFeature {
  type: "Feature";
  id: string;
  properties: Record<string, string>;
  geometry: { type: "Polygon"; coordinates: [number, number][][] };
}

export interface RegionCollection {
  type: "FeatureCollection";
  features: RegionFeature[];
}

export function regionGeoJSON(): RegionCollection {
  return {
    type: "FeatureCollection",
    features: Object.entries(REGION_POLYGONS).map(([id, ring]) => ({
      type: "Feature",
      id,
      properties: { entity: id },
      geometry: { type: "Polygon", coordinates: [[...ring, ring[0]]] },
    })),
  };
}
