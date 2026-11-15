"""GLSL 3.30 core sources for the 3D viewport.

Kept as Python strings on purpose: the app ships as a one-file PyInstaller
executable, so there are no shader data files to locate at run time.

Colour pipeline: vertex colours are sRGB -> linearised in the vertex shader;
lighting happens in linear space; the scene fragment shaders tonemap and
write *linear* [0, 1] values into an sRGB multisampled framebuffer (so the
MSAA resolve is done on display-referred values, which keeps bright edges
anti-aliased); the final composite pass encodes to sRGB by hand because the
QOpenGLWidget's own framebuffer is a plain RGBA8.
"""

from __future__ import annotations

MAX_LINKS = 16
MAX_MATERIALS = 32
MAX_BLOBS = 12
MAX_CONTACTS = 24

_HEADER = "#version 330 core\n"

# ---------------------------------------------------------------------------
# shared chunks
# ---------------------------------------------------------------------------

_COMMON = r"""
const float PI = 3.14159265358979;

vec3 srgbToLinear(vec3 c) {
    return mix(c / 12.92, pow((c + 0.055) / 1.055, vec3(2.4)), step(vec3(0.04045), c));
}
vec3 linearToSrgb(vec3 c) {
    c = max(c, vec3(0.0));
    return mix(c * 12.92, 1.055 * pow(c, vec3(1.0 / 2.4)) - 0.055, step(vec3(0.0031308), c));
}
// Khronos "PBR Neutral" tone mapper: colours below ~0.76 pass through 1:1
// (CAD colours stay true), highlights roll off and desaturate gently.
vec3 tonemap(vec3 color) {
    const float startCompression = 0.8 - 0.04;
    const float desaturation = 0.07;
    color = max(color, vec3(0.0)) * 0.84;                  // exposure: keeps lit plastic saturated
    float x = min(color.r, min(color.g, color.b));
    // (the reference curve subtracts the full 4% "fresnel toe"; a third of it keeps
    // dark parts like the black frame readable against our dark studio)
    float offset = (x < 0.08 ? x - 6.25 * x * x : 0.04) * 0.33;
    color -= offset;
    float peak = max(color.r, max(color.g, color.b));
    if (peak < startCompression) return color;
    float d = 1.0 - startCompression;
    float newPeak = 1.0 - d * d / (peak + d - startCompression);
    color *= newPeak / peak;
    float g = 1.0 - 1.0 / (desaturation * (peak - newPeak) + 1.0);
    return mix(color, vec3(newPeak), g);
}
float ign(vec2 p) {   // interleaved gradient noise
    return fract(52.9829189 * fract(dot(p, vec2(0.06711056, 0.00583715))));
}
vec2 vogel(int i, int n, float phi) {
    float r = sqrt((float(i) + 0.5) / float(n));
    float th = float(i) * 2.39996323 + phi;
    return r * vec2(cos(th), sin(th));
}
"""

_SHADOW = r"""
uniform sampler2DShadow uShadowMap;     // hardware PCF
uniform sampler2D uShadowDepth;         // same texture, raw depth (blocker search)
uniform mat4 uShadowMat;                // world -> shadow uv/depth in [0,1]
uniform float uShadowOn;
uniform vec4 uShadowParams;             // x: frustum size (mm), y: depth range (mm), z: light tan(angular radius), w: min penumbra (uv)

float keyShadow(vec3 wpos, vec3 N, float normalBias) {
    if (uShadowOn < 0.5) return 1.0;
    vec3 p = (uShadowMat * vec4(wpos + N * normalBias, 1.0)).xyz;
    if (p.x <= 0.0 || p.x >= 1.0 || p.y <= 0.0 || p.y >= 1.0 || p.z >= 1.0) return 1.0;
    float phi = ign(gl_FragCoord.xy) * 6.2831853;
    float bias = 0.6 / uShadowParams.y;                     // ~0.6 mm
    float searchR = 9.0 * uShadowParams.z * 6.0 / uShadowParams.x;
    float sum = 0.0, cnt = 0.0;
    for (int i = 0; i < 10; ++i) {
        float d = texture(uShadowDepth, p.xy + vogel(i, 10, phi) * searchR).r;
        if (d < p.z - bias) { sum += d; cnt += 1.0; }
    }
    if (cnt < 0.5) return 1.0;
    float gap = (p.z - sum / cnt) * uShadowParams.y;        // receiver - blocker distance, mm
    float pen = clamp(gap * uShadowParams.z / uShadowParams.x, uShadowParams.w, 0.045);
    float s = 0.0;
    for (int i = 0; i < 16; ++i)
        s += texture(uShadowMap, vec3(p.xy + vogel(i, 16, phi + 1.7) * pen, p.z - bias));
    return s / 16.0;
}
"""

_LIGHTING = r"""
uniform vec3 uEye;
uniform vec3 uKeyDir;   uniform vec3 uKeyColor;
uniform vec3 uFillDir;  uniform vec3 uFillColor;
uniform vec3 uRimDir;   uniform vec3 uRimColor;
uniform vec3 uSkyColor; uniform vec3 uGroundColor; uniform vec3 uHorizonColor;

vec3 brdfLight(vec3 N, vec3 V, vec3 L, vec3 radiance, vec3 diffuse, vec3 F0, float alpha) {
    float NdotL = dot(N, L);
    if (NdotL <= 0.0) return vec3(0.0);
    vec3 H = normalize(V + L);
    float NdotH = max(dot(N, H), 0.0);
    float NdotV = max(dot(N, V), 1e-3);
    float VdotH = max(dot(V, H), 0.0);
    float a2 = alpha * alpha;
    float d = NdotH * NdotH * (a2 - 1.0) + 1.0;
    float D = a2 / (PI * d * d);
    float k = alpha * 0.5;
    float vis = 0.25 / ((NdotL * (1.0 - k) + k) * (NdotV * (1.0 - k) + k));
    vec3 F = F0 + (1.0 - F0) * pow(1.0 - VdotH, 5.0);
    return (diffuse / PI + D * vis * F) * radiance * NdotL;
}

// Procedural studio environment: dark floor, soft grey walls, a big softbox
// over the key side and a strip light on the rim side. Blurred by roughness.
vec3 studioEnv(vec3 R, float rough) {
    float w = mix(0.04, 0.9, rough);
    vec3 col = mix(uGroundColor, uHorizonColor, smoothstep(-w, w * 0.6 + 0.02, R.y));
    col = mix(col, uSkyColor, smoothstep(0.05, 0.95, R.y));
    float sb = mix(0.035, 0.55, rough);
    float key = smoothstep(0.80 - sb, 0.80 + sb, dot(R, uKeyDir));
    float rim = smoothstep(0.90 - sb, 0.90 + sb, dot(R, uRimDir));
    float fill = smoothstep(0.86 - sb, 0.86 + sb, dot(R, uFillDir));
    float gain = 1.0 / (1.0 + 5.0 * rough * rough);
    col += (uKeyColor * 0.55 * key + uRimColor * 0.50 * rim + uFillColor * 0.60 * fill) * gain;
    return col;
}

vec3 hemiAmbient(vec3 N) {
    float t = N.y * 0.5 + 0.5;
    vec3 c = mix(uGroundColor, uHorizonColor, smoothstep(0.0, 0.5, t));
    return mix(c, uSkyColor, smoothstep(0.45, 1.0, t));
}

// Karis' analytic approximation of the split-sum environment BRDF.
vec3 envBRDF(vec3 F0, float rough, float NdotV) {
    const vec4 c0 = vec4(-1.0, -0.0275, -0.572, 0.022);
    const vec4 c1 = vec4(1.0, 0.0425, 1.04, -0.04);
    vec4 r = rough * c0 + c1;
    float a004 = min(r.x * r.x, exp2(-9.28 * NdotV)) * r.x + r.y;
    vec2 ab = vec2(-1.04, 1.04) * a004 + r.zw;
    return F0 * ab.x + ab.y;
}
"""

# ---------------------------------------------------------------------------
# robot geometry (shared vertex stage)
# ---------------------------------------------------------------------------

ROBOT_VS = _HEADER + r"""
layout(location = 0) in vec3 aPos;
layout(location = 1) in vec3 aNormal;
layout(location = 2) in vec4 aColor;
layout(location = 3) in uvec4 aIds;      // link, material, flags, group
uniform mat4 uViewProj;
uniform mat4 uLink[%(MAX_LINKS)d];
out vec3 vWorld;
out vec3 vNormal;
out vec3 vColor;
flat out uint vMat;
flat out uint vLink;
void main() {
    mat4 M = uLink[aIds.x];
    vec4 w = M * vec4(aPos, 1.0);
    vWorld = w.xyz;
    vNormal = mat3(M) * aNormal;
    vColor = aColor.rgb;
    vMat = aIds.y;
    vLink = aIds.x;
    gl_Position = uViewProj * w;
}
""" % {"MAX_LINKS": MAX_LINKS}

ROBOT_FS = _HEADER + _COMMON + _LIGHTING + _SHADOW + r"""
in vec3 vWorld;
in vec3 vNormal;
in vec3 vColor;
flat in uint vMat;
flat in uint vLink;
uniform vec4 uMaterial[%(MAX_MATERIALS)d];   // roughness, metallic, emissive, tintable
uniform vec4 uLinkGlow[%(MAX_LINKS)d];       // rgb (linear), strength
uniform vec4 uTint;                          // rgb (linear), enabled
uniform float uGhost;                        // 1 = translucent "x-ray" shell
out vec4 fragColor;

void main() {
    vec4 mat = uMaterial[vMat];
    vec3 albedo = max(srgbToLinear(vColor), vec3(0.028));   // no real plastic is blacker than this
    if (uTint.a > 0.5 && mat.w > 0.5) {
        // keep the part-to-part value differences of the CAD colours, swap the hue
        // (0.324 = luminance of the stock orange: dark parts stay dark shades of the tint)
        float l = dot(albedo, vec3(0.2126, 0.7152, 0.0722));
        albedo = uTint.rgb * clamp(l / 0.324, 0.10, 1.25);
    }
    float rough = clamp(mat.x, 0.06, 1.0);
    float metal = clamp(mat.y, 0.0, 1.0);
    vec3 V = normalize(uEye - vWorld);
    vec3 N = normalize(vNormal);
    if (dot(N, V) < 0.0) {
        if (uGhost > 0.5) discard;           // back faces of the ghost shell
        N = -N;                              // two-sided lighting for open CAD shells
    }
    float NdotV = max(dot(N, V), 1e-3);
    vec3 F0 = mix(vec3(0.04), albedo, metal);
    vec3 diffuse = albedo * (1.0 - metal);
    float alpha = rough * rough;

    float sh = keyShadow(vWorld, N, 0.5);
    vec3 col = brdfLight(N, V, uKeyDir, uKeyColor * sh, diffuse, F0, alpha);
    col += brdfLight(N, V, uFillDir, uFillColor, diffuse, F0, alpha);
    col += brdfLight(N, V, uRimDir, uRimColor, diffuse, F0, alpha);

    // image-based-ish ambient: hemisphere irradiance + procedural studio reflection
    float occl = mix(0.72, 1.0, sh);         // key shadow also dims the bounce a little
    col += diffuse * hemiAmbient(N) * occl;
    vec3 R = reflect(-V, N);
    // (rough printed plastic keeps its hue: less white sheen than a polished part)
    col += studioEnv(R, rough) * envBRDF(F0, rough, NdotV) * mix(0.6, 1.0, sh) * mix(1.0, 0.5, rough * (1.0 - metal));

    col += albedo * mat.z;                   // emissive materials

    vec4 glow = uLinkGlow[vLink];
    float fres = pow(1.0 - NdotV, 2.5);
    col += glow.rgb * glow.a * (0.018 + 0.42 * fres);

    float a = 1.0;
    if (uGhost > 0.5) {
        a = mix(0.10, 0.62, pow(1.0 - NdotV, 1.6));
        col = mix(col, vec3(0.75, 0.85, 1.0) * 0.5, 0.25);
    }
    fragColor = vec4(tonemap(col), a);
}
""" % {"MAX_MATERIALS": MAX_MATERIALS, "MAX_LINKS": MAX_LINKS}

DEPTH_FS = _HEADER + r"""
void main() { }
"""

PICK_FS = _HEADER + r"""
flat in uint vLink;
out vec4 fragColor;
void main() { fragColor = vec4((float(vLink) + 1.0) / 255.0, 0.0, 0.0, 1.0); }
"""

MASK_FS = _HEADER + r"""
uniform vec4 uMaskValue;
out vec4 fragColor;
void main() { fragColor = uMaskValue; }
"""

# ---------------------------------------------------------------------------
# background + floor
# ---------------------------------------------------------------------------

FULLSCREEN_VS = _HEADER + r"""
out vec2 vUV;
void main() {
    vec2 p = vec2(float((gl_VertexID << 1) & 2), float(gl_VertexID & 2));
    vUV = p;
    gl_Position = vec4(p * 2.0 - 1.0, 0.0, 1.0);
}
"""

BACKGROUND_FS = _HEADER + _COMMON + r"""
in vec2 vUV;
uniform vec3 uTop;       // linear
uniform vec3 uBottom;
uniform vec3 uGlow;
uniform float uHorizon;  // screen-space y (0..1) of the horizon line
out vec4 fragColor;
void main() {
    vec3 col = mix(uBottom, uTop, smoothstep(0.0, 1.0, vUV.y));
    // soft pool of light behind the subject, centred a little below the horizon
    vec2 d = (vUV - vec2(0.5, clamp(uHorizon, 0.2, 0.8) - 0.08)) * vec2(1.0, 1.35);
    col += uGlow * exp(-dot(d, d) * 5.5);
    // faint haze along the horizon so the far edge of the floor melts into the backdrop
    float hy = (vUV.y - uHorizon) / 0.085;
    float hx = (vUV.x - 0.5) / 0.62;
    col += uGlow * 1.6 * exp(-hy * hy) * exp(-hx * hx);
    vec2 q = vUV - 0.5;
    col *= 1.0 - 0.55 * smoothstep(0.25, 0.95, dot(q, q) * 2.0);
    col += (ign(gl_FragCoord.xy) - 0.5) / 255.0 * 0.6;
    fragColor = vec4(max(col, 0.0), 1.0);
}
"""

FLOOR_VS = _HEADER + r"""
layout(location = 0) in vec2 aPos;
uniform mat4 uViewProj;
uniform vec3 uFocus;       // xz centre of the floor disc
uniform float uExtent;
out vec3 vWorld;
void main() {
    vWorld = vec3(uFocus.x + aPos.x * uExtent, 0.0, uFocus.z + aPos.y * uExtent);
    gl_Position = uViewProj * vec4(vWorld, 1.0);
}
"""

FLOOR_FS = _HEADER + _COMMON + _LIGHTING + _SHADOW + r"""
in vec3 vWorld;
uniform vec3 uFocus;
uniform float uFadeRadius;
uniform float uGridOn;
uniform vec3 uFloorColor;       // linear albedo
uniform vec3 uGridColor;        // linear
uniform vec3 uAccent;           // linear
uniform int uBlobCount;
uniform vec4 uBlobs[%(MAX_BLOBS)d];        // x, z, radius, strength
uniform int uContactCount;
uniform vec4 uContacts[%(MAX_CONTACTS)d];  // x, z, radius, strength
out vec4 fragColor;

float gridLine(vec2 p, float cell, float px) {
    vec2 c = p / cell;
    vec2 fw = max(fwidth(c), vec2(1e-6));
    vec2 g = abs(fract(c - 0.5) - 0.5) / fw;
    float line = 1.0 - clamp(min(g.x, g.y) - (px - 1.0) * 0.5, 0.0, 1.0);
    // fade the lines out before they alias into moire
    return line * (1.0 - smoothstep(0.18, 0.5, max(fw.x, fw.y)));
}

void main() {
    vec2 p = vWorld.xz;
    float dist = length(p - uFocus.xz);
    float fade = 1.0 - smoothstep(uFadeRadius * 0.30, uFadeRadius, dist);
    if (fade <= 0.001) discard;
    vec3 V = normalize(uEye - vWorld);
    float below = step(uEye.y, 0.0);                 // camera under the floor
    vec3 N = vec3(0.0, 1.0, 0.0);

    float sh = keyShadow(vWorld, N, 0.15);
    // contact / ambient darkening under the body and feet
    float ao = 1.0;
    for (int i = 0; i < uBlobCount; ++i) {
        vec2 d = p - uBlobs[i].xy;
        ao *= 1.0 - uBlobs[i].w * exp(-dot(d, d) / (uBlobs[i].z * uBlobs[i].z));
    }

    vec3 albedo = uFloorColor;
    float pool = exp(-dist * dist / (uFadeRadius * uFadeRadius * 0.22));   // key light pool
    vec3 light = uKeyColor * max(uKeyDir.y, 0.0) / PI * mix(0.10, 1.0, pool) * sh
               + hemiAmbient(N) * mix(0.35, 0.80, pool) * ao * mix(0.8, 1.0, sh);
    vec3 col = albedo * light;

    // faint glossy sheen of the studio lights
    vec3 R = reflect(-V, N);
    float NdotV = max(dot(N, V), 1e-3);
    col += studioEnv(R, 0.38) * envBRDF(vec3(0.04), 0.38, NdotV) * 0.30 * ao * mix(0.35, 1.0, sh) * mix(0.3, 1.0, pool);

    if (uGridOn > 0.5) {
        float minor = gridLine(p, 10.0, 1.0);
        float major = gridLine(p, 50.0, 1.25);
        float g = max(minor * 0.17, major * 0.40) * mix(0.45, 1.0, pool);
        // origin axes, slightly stronger
        vec2 fwp = max(fwidth(p), vec2(1e-6));
        float ax = 1.0 - clamp(min(abs(p.x) / fwp.x, abs(p.y) / fwp.y) - 0.25, 0.0, 1.0);
        g = max(g, ax * 0.55 * mix(0.45, 1.0, pool));
        col = mix(col, uGridColor * (0.35 + 0.65 * sh) * mix(0.6, 1.0, ao), g * mix(1.0, 0.35, below));
    }

    // touch-down rings where the robot is in contact with the ground
    for (int i = 0; i < uContactCount; ++i) {
        float r = length(p - uContacts[i].xy);
        float w = max(fwidth(r), 1e-4);
        float ring = 1.0 - smoothstep(0.0, w * 1.5 + 0.25, abs(r - uContacts[i].z));
        float disc = 1.0 - smoothstep(uContacts[i].z * 0.2, uContacts[i].z, r);
        col += uAccent * (ring * 0.30 + disc * 0.05) * uContacts[i].w;
    }

    col = tonemap(col);
    fragColor = vec4(col, fade * mix(1.0, 0.45, below));
}
""" % {"MAX_BLOBS": MAX_BLOBS, "MAX_CONTACTS": MAX_CONTACTS}

# ---------------------------------------------------------------------------
# OLED screen
# ---------------------------------------------------------------------------

SCREEN_VS = _HEADER + r"""
layout(location = 0) in vec3 aPos;     // bind pose, body frame
layout(location = 1) in vec2 aUV;
uniform mat4 uViewProj;
uniform mat4 uModel;
out vec2 vUV;
out vec3 vWorld;
void main() {
    vec4 w = uModel * vec4(aPos, 1.0);
    vWorld = w.xyz;
    vUV = aUV;
    gl_Position = uViewProj * w;
}
"""

SCREEN_FS = _HEADER + _COMMON + _LIGHTING + r"""
in vec2 vUV;
in vec3 vWorld;
uniform sampler2D uFace;      // R8, 128x64, row 0 = top, mipmapped
uniform vec3 uNormal;         // world
uniform vec3 uLitColor;       // linear emissive of a lit pixel
uniform float uGlowPass;      // 1: soft additive halo around the panel
uniform vec2 uFaceSize;
out vec4 fragColor;
void main() {
    if (uGlowPass > 0.5) {
        float g = textureLod(uFace, vUV, 3.2).r * 0.55 + textureLod(uFace, vUV, 4.6).r * 0.45;
        vec2 e = smoothstep(vec2(-0.22), vec2(0.02), vUV) * (1.0 - smoothstep(vec2(0.98), vec2(1.22), vUV));
        vec3 c = uLitColor * g * 0.11 * e.x * e.y;
        fragColor = vec4(tonemap(c), 0.0);          // additive (ONE, ONE)
        return;
    }
    vec2 px = vUV * uFaceSize;
    vec2 fw = max(fwidth(px), vec2(1e-5));
    // crisp-but-antialiased texel lookup (bilinear only across the 1-pixel seam)
    vec2 uvc = (floor(px) + 0.5 + clamp((fract(px) - 0.5) / fw, -0.5, 0.5)) / uFaceSize;
    float lit = texture(uFace, uvc).r;
    // sub-pixel structure, only when zoomed in far enough to resolve it
    vec2 cell = abs(fract(px) - 0.5);
    float gap = 1.0 - smoothstep(0.36, 0.5, max(cell.x, cell.y));
    float detail = clamp(1.0 - max(fw.x, fw.y) * 3.0, 0.0, 1.0);
    lit *= mix(1.0, 0.25 + 0.75 * gap, detail);

    vec3 V = normalize(uEye - vWorld);
    vec3 N = normalize(uNormal);
    float NdotV = max(dot(N, V), 1e-3);
    vec3 R = reflect(-V, N);
    vec3 glass = vec3(0.0035, 0.0042, 0.0058)
               + studioEnv(R, 0.10) * envBRDF(vec3(0.045), 0.10, NdotV) * 0.38;
    // OLEDs dim slightly off-axis
    vec3 col = glass + uLitColor * lit * mix(0.75, 1.0, NdotV);
    fragColor = vec4(tonemap(col), 1.0);
}
"""

# ---------------------------------------------------------------------------
# rotation gizmo: one quad in the joint's rotation plane, shapes are SDFs
# ---------------------------------------------------------------------------

GIZMO_VS = _HEADER + r"""
layout(location = 0) in vec2 aPos;       // -1..1
uniform mat4 uViewProj;
uniform vec3 uCenter;
uniform vec3 uU;                         // world vector of plane x (= current limb direction) * radius
uniform vec3 uV;                         // world vector of plane y * radius
out vec2 vP;
void main() {
    vP = aPos * 1.45;
    gl_Position = uViewProj * vec4(uCenter + uU * vP.x + uV * vP.y, 1.0);
}
"""

GIZMO_FS = _HEADER + _COMMON + r"""
in vec2 vP;
uniform float uCur;        // current servo angle (deg)
uniform vec2 uRange;       // allowed servo range (deg)
uniform float uDir;        // +1 / -1: plane-angle direction of increasing servo angle
uniform vec3 uAccent;      // linear
uniform float uAlpha;      // overall opacity (occluded pass draws dimmer)
uniform float uHot;        // 0..1 handle hover / drag emphasis
out vec4 fragColor;

float aa(float d, float w) { return clamp(0.5 - d / max(w, 1e-6), 0.0, 1.0); }
void put(inout vec4 acc, vec3 color, float a) { acc = acc * (1.0 - a) + vec4(color * a, a); }

void main() {
    float r = length(vP);
    float px = max(length(fwidth(vP)) * 0.7071, 1e-5);        // one pixel in plane units
    float phi = degrees(atan(vP.y, vP.x));                    // plane angle from the limb direction
    float servo = uCur + phi * uDir;                          // servo angle this fragment represents
    float degPx = degrees(px / max(r, 1e-3));                 // one pixel in degrees at this radius
    float inRange = aa(max(uRange.x - servo, servo - uRange.y), degPx);
    float inRangeWide = aa(max(uRange.x - servo, servo - uRange.y) - degPx * 1.5, degPx);
    float lo = min(uRange.x, uCur), hi = max(uRange.x, uCur);
    float inWedge = aa(max(lo - servo, servo - hi), degPx);

    const vec3 white = vec3(0.94, 0.96, 1.0);
    const vec3 ink = vec3(0.012, 0.013, 0.018);
    vec4 acc = vec4(0.0);                                     // premultiplied accumulation
    float ringD = abs(r - 1.0);

    // filled wedge from the range start to the current angle
    put(acc, mix(white, uAccent, 0.35), inWedge * aa(r - 1.0, px) * 0.20);

    // dark under-strokes so the white ink reads on bright plastic too
    put(acc, ink, aa(ringD - px * 2.4, px * 1.5) * inRangeWide * 0.38);
    put(acc, ink, aa(ringD - px * 3.4, px * 1.5) * inWedge * 0.30);
    float spokeD = max(abs(vP.y), max(0.06 - vP.x, vP.x - 1.0));
    put(acc, ink, aa(spokeD - px * 2.2, px * 1.5) * 0.40);

    // range track + thicker progress arc
    put(acc, white, aa(ringD - px * 0.8, px) * inRange * 0.60);
    put(acc, white, aa(ringD - px * 1.8, px) * inWedge);

    // tick marks every 15 deg (longer every 45)
    float t15 = abs(mod(servo + 7.5, 15.0) - 7.5);
    float t45 = abs(mod(servo + 22.5, 45.0) - 22.5);
    float tick = aa(t15 - degPx * 0.7, degPx) * aa(max(1.0 - r + px * 3.0, r - 1.085), px) * 0.60;
    tick = max(tick, aa(t45 - degPx * 0.9, degPx) * aa(max(1.0 - r + px * 3.0, r - 1.15), px) * 0.90);
    put(acc, white, tick * inRange);

    // end stops
    float e0 = abs(servo - uRange.x), e1 = abs(servo - uRange.y);
    put(acc, white, aa(min(e0, e1) - degPx * 1.0, degPx) * aa(max(0.86 - r, r - 1.15), px) * 0.95);

    // current-angle spoke and pivot dot
    put(acc, white, aa(spokeD - px * 0.9, px) * 0.95);
    put(acc, ink, aa(r - px * 5.0, px * 1.5) * 0.45);
    put(acc, white, aa(r - px * 3.5, px));

    // handle: soft halo, dark rim, accent ring, white core
    float hr = px * mix(6.5, 8.5, uHot);
    float hd = length(vP - vec2(1.0, 0.0));
    put(acc, uAccent, aa(hd - hr - px * 6.0, px * 7.0) * 0.40 * uHot);
    put(acc, ink, aa(hd - hr - px * 1.6, px * 1.5) * 0.55);
    put(acc, uAccent, aa(hd - hr, px));
    put(acc, white, aa(hd - hr + px * 2.4, px));

    acc *= uAlpha;
    if (acc.a < 0.003) discard;
    fragColor = acc;                                          // premultiplied
}
"""

# ---------------------------------------------------------------------------
# composite (scene + outlines -> widget framebuffer) and 2D sprites
# ---------------------------------------------------------------------------

COMPOSITE_FS = _HEADER + _COMMON + r"""
in vec2 vUV;
uniform sampler2D uScene;      // sRGB texture -> linear
uniform sampler2D uMask;       // r: hover silhouette, g: selected silhouette
uniform vec2 uTexel;           // 1 / framebuffer size
uniform float uOutlinePx;      // outline width in framebuffer pixels
uniform vec3 uHoverColor;      // linear
uniform vec3 uSelectColor;     // linear
uniform float uMaskOn;
uniform sampler2D uAO;         // screen-space ambient occlusion (noisy, blurred here)
uniform float uAOStrength;
out vec4 fragColor;
void main() {
    vec3 col = texture(uScene, vUV).rgb;
    if (uAOStrength > 0.0) {
        float ao = 0.0;
        for (int y = -1; y <= 2; ++y)
            for (int x = -1; x <= 2; ++x)
                ao += texture(uAO, vUV + (vec2(float(x), float(y)) - 0.5) * uTexel * 1.5).r;
        ao /= 16.0;
        col *= mix(1.0, ao, uAOStrength);
    }
    if (uMaskOn > 0.5) {
        vec2 centre = texture(uMask, vUV).rg;
        vec2 ringSum = vec2(0.0);
        vec2 ringMax = vec2(0.0);
        vec2 farMax = vec2(0.0);
        const int N = 16;
        for (int i = 0; i < N; ++i) {
            float a = (float(i) + 0.5) * 6.2831853 / float(N);
            vec2 dir = vec2(cos(a), sin(a));
            vec2 s1 = texture(uMask, vUV + dir * uTexel * uOutlinePx).rg;
            vec2 s2 = texture(uMask, vUV + dir * uTexel * uOutlinePx * 0.5).rg;
            vec2 s3 = texture(uMask, vUV + dir * uTexel * (uOutlinePx + 1.6)).rg;
            ringSum += s1 + s2;
            ringMax = max(ringMax, max(s1, s2));
            farMax = max(farMax, s3);
        }
        vec2 cover = clamp(ringSum / float(N) * 1.6, 0.0, 1.0);
        cover = max(cover, ringMax * 0.85);
        vec2 edge = cover * (1.0 - centre);
        // thin dark rim outside the bright line: keeps it readable on any colour
        float rim = max(farMax.r, farMax.g) * (1.0 - max(centre.r, centre.g));
        col *= 1.0 - 0.50 * rim * (1.0 - max(edge.r, edge.g));
        col = mix(col, uSelectColor, centre.g * 0.035);
        col = mix(col, uHoverColor, edge.r * 0.90 * (1.0 - edge.g));
        col = mix(col, uSelectColor, edge.g);
    }
    col = linearToSrgb(col);
    col += (ign(gl_FragCoord.xy + 17.0) - 0.5) / 255.0;
    fragColor = vec4(col, 1.0);
}
"""

SPRITE_VS = _HEADER + r"""
uniform vec4 uRect;        // x, y, w, h in framebuffer pixels (top-left origin)
uniform vec2 uViewport;    // framebuffer size
uniform sampler2D uIds;    // link-id buffer (R8, id+1)
uniform vec4 uAnchor;      // xy: uv in the id buffer, z: expected id value (0..1), w: 1 = test
uniform vec2 uIdTexel;
uniform float uOpacity;
out vec2 vUV;
out float vOpacity;
void main() {
    vec2 p = vec2(float(gl_VertexID & 1), float((gl_VertexID >> 1) & 1));
    vUV = p;
    float vis = 1.0;
    if (uAnchor.w > 0.5) {
        vis = 0.0;
        const vec2 taps[5] = vec2[5](vec2(0.0), vec2(3.0, 0.0), vec2(-3.0, 0.0), vec2(0.0, 3.0), vec2(0.0, -3.0));
        for (int i = 0; i < 5; ++i)
            vis += step(abs(texture(uIds, uAnchor.xy + taps[i] * uIdTexel).r - uAnchor.z), 0.5 / 255.0);
        vis = min(vis / 3.0, 1.0);           // 3 of 5 taps visible = fully shown
    }
    vOpacity = uOpacity * mix(0.30, 1.0, vis);
    vec2 px = uRect.xy + p * uRect.zw;
    gl_Position = vec4(px.x / uViewport.x * 2.0 - 1.0, 1.0 - px.y / uViewport.y * 2.0, 0.0, 1.0);
}
"""

SPRITE_FS = _HEADER + r"""
in vec2 vUV;
in float vOpacity;
uniform sampler2D uTex;    // premultiplied RGBA
out vec4 fragColor;
void main() { fragColor = texture(uTex, vUV) * vOpacity; }
"""

# ---------------------------------------------------------------------------
# screen-space ambient occlusion (Scalable Ambient Obscurance estimator)
# ---------------------------------------------------------------------------

AO_FS = _HEADER + _COMMON + r"""
in vec2 vUV;
uniform sampler2D uDepth;      // resolved scene depth
uniform mat4 uInvProj;
uniform vec2 uTexel;
uniform float uRadius;         // world units (mm)
uniform float uProjScale;      // pixels per world unit at view depth 1
out vec4 fragColor;

vec3 viewPos(vec2 uv) {
    float d = texture(uDepth, uv).r;
    vec4 p = uInvProj * vec4(uv * 2.0 - 1.0, d * 2.0 - 1.0, 1.0);
    return p.xyz / p.w;
}

void main() {
    float d = texture(uDepth, vUV).r;
    if (d >= 0.99999) { fragColor = vec4(1.0); return; }
    vec3 P = viewPos(vUV);
    vec3 Pr = viewPos(vUV + vec2(uTexel.x, 0.0)), Pl = viewPos(vUV - vec2(uTexel.x, 0.0));
    vec3 Pu = viewPos(vUV + vec2(0.0, uTexel.y)), Pd = viewPos(vUV - vec2(0.0, uTexel.y));
    vec3 dx = abs(Pr.z - P.z) < abs(P.z - Pl.z) ? Pr - P : P - Pl;
    vec3 dy = abs(Pu.z - P.z) < abs(P.z - Pd.z) ? Pu - P : P - Pd;
    vec3 N = normalize(cross(dx, dy));
    float rPix = clamp(uRadius * uProjScale / max(-P.z, 1.0), 2.0, 96.0);
    float phi = ign(gl_FragCoord.xy) * 6.2831853;
    float r2 = uRadius * uRadius;
    float bias = 0.012 * uRadius - P.z * 0.0006;
    float sum = 0.0;
    const int NS = 12;
    for (int i = 0; i < NS; ++i) {
        vec3 S = viewPos(vUV + vogel(i, NS, phi) * rPix * uTexel);
        vec3 v = S - P;
        float vv = dot(v, v);
        float vn = dot(v, N);
        float f = max(r2 - vv, 0.0);
        sum += f * f * f * max((vn - bias) / (vv + 0.02 * r2), 0.0);
    }
    float ao = max(0.0, 1.0 - sum * 5.0 / (r2 * r2 * r2) * (1.35 / float(NS)) * uRadius);
    fragColor = vec4(ao);
}
"""
