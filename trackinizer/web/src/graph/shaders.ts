import { HALO_GAP_PX } from "./constants";

export const NODE_FLOATS = 23; // pos(2) + radius(1) + fill(4) + wash(4) + ring(4) + dot(4) + halo(4)
export const LINK_STRIDE_FLOATS = 11; // src(2) + dst(2) + targetR(1) + width(1) + dash(1) + color(4)

export const NODE_VERT = `#version 300 es
precision highp float;
layout(location = 0) in vec2 a_quad;
layout(location = 1) in vec2 a_pos;
layout(location = 2) in float a_radius;
layout(location = 3) in vec4 a_fill;
layout(location = 4) in vec4 a_wash;
layout(location = 5) in vec4 a_ring;
layout(location = 6) in vec4 a_dot;
layout(location = 7) in vec4 a_halo;

uniform vec3 u_cam;
uniform vec2 u_res;

out vec2 v_local;
out float v_radius;
out float v_aa;
out float v_haloGap;
out float v_haloW;
out float v_ringW;
out vec4 v_fill;
out vec4 v_wash;
out vec4 v_ring;
out vec4 v_dot;
out vec4 v_halo;

void main() {
  float zoom = u_cam.z;
  float haloGap = ${HALO_GAP_PX.toFixed(1)} / zoom;
  float haloW = 2.0 / zoom;
  float ringW = 1.5 / zoom;
  float pad = a_halo.a > 0.0 ? (haloGap + haloW + 2.0 / zoom) : (ringW + 2.0 / zoom);
  float extent = a_radius + pad;
  vec2 world = a_pos + a_quad * extent;
  vec2 screen = (world - u_cam.xy) * zoom + u_res * 0.5;
  vec2 ndc = (screen / u_res) * 2.0 - 1.0;
  gl_Position = vec4(ndc.x, -ndc.y, 0.0, 1.0);
  v_local = a_quad * extent;
  v_radius = a_radius;
  v_aa = 1.0 / zoom;
  v_haloGap = haloGap;
  v_haloW = haloW;
  v_ringW = ringW;
  v_fill = a_fill;
  v_wash = a_wash;
  v_ring = a_ring;
  v_dot = a_dot;
  v_halo = a_halo;
}`;

export const NODE_FRAG = `#version 300 es
precision highp float;
in vec2 v_local;
in float v_radius;
in float v_aa;
in float v_haloGap;
in float v_haloW;
in float v_ringW;
in vec4 v_fill;
in vec4 v_wash;
in vec4 v_ring;
in vec4 v_dot;
in vec4 v_halo;

out vec4 outColor;

vec4 over(vec4 dst, vec4 src) {
  float a = src.a + dst.a * (1.0 - src.a);
  if (a <= 0.0) return vec4(0.0);
  vec3 rgb = (src.rgb * src.a + dst.rgb * dst.a * (1.0 - src.a)) / a;
  return vec4(rgb, a);
}

void main() {
  float r = length(v_local);
  float aa = v_aa;
  vec4 col = vec4(0.0);

  if (v_halo.a > 0.0) {
    float hOuter = v_radius + v_haloGap + v_haloW * 0.5;
    float hInner = v_radius + v_haloGap - v_haloW * 0.5;
    float hMask = smoothstep(hInner - aa, hInner + aa, r) * (1.0 - smoothstep(hOuter - aa, hOuter + aa, r));
    if (hMask > 0.0) col = over(col, vec4(v_halo.rgb, v_halo.a * hMask));
  }

  float disc = 1.0 - smoothstep(v_radius - aa, v_radius + aa, r);
  if (disc > 0.0) {
    if (v_fill.a > 0.0) col = over(col, vec4(v_fill.rgb, v_fill.a * disc));
    if (v_wash.a > 0.0) col = over(col, vec4(v_wash.rgb, v_wash.a * disc));
    float ringInner = max(0.0, v_radius - v_ringW);
    float ringMask = smoothstep(ringInner - aa, ringInner + aa, r) * disc;
    if (v_ring.a > 0.0 && ringMask > 0.0) {
      col = over(col, vec4(v_ring.rgb, v_ring.a * ringMask));
    }
    if (v_dot.a > 0.0) {
      float dotR = v_radius * 0.4;
      float dotMask = 1.0 - smoothstep(dotR - aa, dotR + aa, r);
      if (dotMask > 0.0) col = over(col, vec4(v_dot.rgb, v_dot.a * dotMask));
    }
  }

  if (col.a <= 0.001) discard;
  outColor = col;
}`;

export const LINK_VERT = `#version 300 es
precision highp float;
layout(location = 0) in vec2 a_quad;
layout(location = 1) in vec2 a_src;
layout(location = 2) in vec2 a_dst;
layout(location = 3) in float a_targetR;
layout(location = 4) in float a_width;
layout(location = 5) in float a_dash;
layout(location = 6) in vec4 a_color;

uniform vec3 u_cam;
uniform vec2 u_res;
uniform float u_arrowLen;

out float v_along;
out float v_across;
out float v_halfW;
out float v_dashOn;
out float v_dashOff;
out vec4 v_color;

void main() {
  vec2 delta = a_dst - a_src;
  float len = length(delta);
  vec2 dir = len > 1e-4 ? delta / len : vec2(1.0, 0.0);
  vec2 perp = vec2(-dir.y, dir.x);
  float trim = a_targetR + u_arrowLen * 0.5;
  float segLen = max(0.0, len - trim);
  float effW = min(max(a_width, 1.0), 2.25);
  float padW = effW + 2.0;
  float w = padW / u_cam.z;
  vec2 world = a_src + dir * (a_quad.x * segLen) + perp * (a_quad.y * w);
  vec2 screen = (world - u_cam.xy) * u_cam.z + u_res * 0.5;
  vec2 ndc = (screen / u_res) * 2.0 - 1.0;
  gl_Position = vec4(ndc.x, -ndc.y, 0.0, 1.0);
  v_along = a_quad.x * segLen * u_cam.z;
  v_across = a_quad.y * padW;
  v_halfW = effW * 0.5;
  v_dashOn = a_dash > 0.0 ? max(a_dash, a_dash * u_cam.z) : 0.0;
  v_dashOff = a_dash > 0.0 ? max(3.0, 3.0 * u_cam.z) : 0.0;
  v_color = vec4(a_color.rgb, a_color.a * min(1.0, a_width));
}`;

export const LINK_FRAG = `#version 300 es
precision highp float;
in float v_along;
in float v_across;
in float v_halfW;
in float v_dashOn;
in float v_dashOff;
in vec4 v_color;
out vec4 outColor;

void main() {
  float edgeAlpha = clamp(v_halfW + 0.5 - abs(v_across), 0.0, 1.0);
  float dashAlpha = 1.0;
  if (v_dashOn > 0.0) {
    float period = v_dashOn + v_dashOff;
    float phase = mod(v_along, period);
    dashAlpha = clamp(phase + 0.5, 0.0, 1.0) * clamp(v_dashOn + 0.5 - phase, 0.0, 1.0);
  }
  float a = v_color.a * edgeAlpha * dashAlpha;
  if (a <= 0.002) discard;
  outColor = vec4(v_color.rgb, a);
}`;

export const ARROW_VERT = `#version 300 es
precision highp float;
layout(location = 0) in vec2 a_tri;
layout(location = 1) in vec2 a_src;
layout(location = 2) in vec2 a_dst;
layout(location = 3) in float a_targetR;
layout(location = 4) in vec4 a_color;

uniform vec3 u_cam;
uniform vec2 u_res;
uniform float u_arrowLen;

out vec4 v_color;

void main() {
  vec2 delta = a_dst - a_src;
  float len = length(delta);
  if (len <= a_targetR + 1e-3 || u_arrowLen <= 0.0) {
    gl_Position = vec4(2.0, 2.0, 0.0, 1.0);
    v_color = vec4(0.0);
    return;
  }
  vec2 dir = delta / len;
  vec2 perp = vec2(-dir.y, dir.x);
  vec2 tip = a_dst - dir * a_targetR;
  vec2 world = tip + dir * (a_tri.x * u_arrowLen) + perp * (a_tri.y * u_arrowLen * 0.85);
  vec2 screen = (world - u_cam.xy) * u_cam.z + u_res * 0.5;
  vec2 ndc = (screen / u_res) * 2.0 - 1.0;
  gl_Position = vec4(ndc.x, -ndc.y, 0.0, 1.0);
  v_color = a_color;
}`;

export const ARROW_FRAG = `#version 300 es
precision highp float;
in vec4 v_color;
out vec4 outColor;
void main() {
  if (v_color.a <= 0.0) discard;
  outColor = v_color;
}`;

export function compileProgram(
  gl: WebGL2RenderingContext,
  vertSrc: string,
  fragSrc: string,
): WebGLProgram {
  const compile = (type: number, src: string) => {
    const sh = gl.createShader(type);
    if (!sh) throw new Error("Failed to create shader");
    gl.shaderSource(sh, src);
    gl.compileShader(sh);
    if (!gl.getShaderParameter(sh, gl.COMPILE_STATUS)) {
      const info = gl.getShaderInfoLog(sh) ?? "shader error";
      gl.deleteShader(sh);
      throw new Error(info);
    }
    return sh;
  };
  const vs = compile(gl.VERTEX_SHADER, vertSrc);
  const fs = compile(gl.FRAGMENT_SHADER, fragSrc);
  const prog = gl.createProgram();
  if (!prog) throw new Error("Failed to create program");
  gl.attachShader(prog, vs);
  gl.attachShader(prog, fs);
  gl.linkProgram(prog);
  gl.deleteShader(vs);
  gl.deleteShader(fs);
  if (!gl.getProgramParameter(prog, gl.LINK_STATUS)) {
    throw new Error(gl.getProgramInfoLog(prog) ?? "link error");
  }
  return prog;
}

export function createStaticBuffer(
  gl: WebGL2RenderingContext,
  data: Float32Array,
): WebGLBuffer {
  const buf = gl.createBuffer();
  if (!buf) throw new Error("Failed to create buffer");
  gl.bindBuffer(gl.ARRAY_BUFFER, buf);
  gl.bufferData(gl.ARRAY_BUFFER, data, gl.STATIC_DRAW);
  return buf;
}

export function createInstancedVao(
  gl: WebGL2RenderingContext,
  geomBuf: WebGLBuffer,
  instBuf: WebGLBuffer,
  strideFloats: number,
  attributes: readonly (readonly [loc: number, size: number, offsetFloats: number])[],
): WebGLVertexArrayObject {
  const vao = gl.createVertexArray();
  if (!vao) throw new Error("Failed to create VAO");
  gl.bindVertexArray(vao);
  gl.bindBuffer(gl.ARRAY_BUFFER, geomBuf);
  gl.enableVertexAttribArray(0);
  gl.vertexAttribPointer(0, 2, gl.FLOAT, false, 0, 0);
  gl.bindBuffer(gl.ARRAY_BUFFER, instBuf);
  for (const [loc, size, off] of attributes) {
    gl.enableVertexAttribArray(loc);
    gl.vertexAttribPointer(loc, size, gl.FLOAT, false, strideFloats * 4, off * 4);
    gl.vertexAttribDivisor(loc, 1);
  }
  gl.bindVertexArray(null);
  return vao;
}
