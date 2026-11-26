/** Shared physics, clustering, and camera framing constants across graph renderers. */

export const LINK_STRENGTH = 0.6;
export const CROSS_ISLAND_STRENGTH = 0.05;

export const FORCE_CHARGE = -70;
export const FORCE_DISTANCE_MAX = 280;
export const FORCE_LINK_DISTANCE = 55;
export const FORCE_GRAVITY = 0.02;
export const FORCE_VELOCITY_DECAY = 0.58;
export const FORCE_ALPHA_DECAY = 0.04;
export const FORCE_ALPHA_MIN = 0.001;

export const CLUSTER_PULL = 0.1;
export const CLUSTER_PUSH = 0.5;
export const CLUSTER_SPACING = 30;
export const GOLDEN_ANGLE = Math.PI * (3 - Math.sqrt(5));

export const FIT_MS = 400;
export const FIT_MAX_ZOOM = 4;
export const ZOOM_MS = 200;
export const MOVE_MS = 600;
export const CLOSE_ZOOM = 2.5;
export const FIT_PADDING_PX = 70;
export const HALO_GAP_PX = 3.5;
export const LABEL_GAP_PX = 3;
export const LABEL_PX = 11;
