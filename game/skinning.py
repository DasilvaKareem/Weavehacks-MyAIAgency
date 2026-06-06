"""GPU vertex-skinning shader for animated glTF characters.

raylib 5.5 loads glTF skinned meshes with per-vertex bone IDs/weights for GPU
skinning. The CPU path (`update_model_animation`) fights that setup and explodes
the mesh into "squiggly" spikes. The fix is to drive animation through
`update_model_animation_bones` + a shader that reads `boneMatrices` and skins on
the GPU. This module builds that shader and assigns it to a model's materials.

macOS reports GL 4.1 but raylib emits #version 330 GLSL, so we match that.
"""
from __future__ import annotations

import pyray as pr

_VS = """#version 330
in vec3 vertexPosition;
in vec2 vertexTexCoord;
in vec3 vertexNormal;
in vec4 vertexColor;
in vec4 vertexBoneIds;
in vec4 vertexBoneWeights;

#define MAX_BONE_NUM 128
uniform mat4 boneMatrices[MAX_BONE_NUM];
uniform mat4 mvp;
uniform mat4 matModel;
uniform mat4 matNormal;

out vec2 fragTexCoord;
out vec4 fragColor;
out vec3 fragNormal;

void main()
{
    int b0 = int(vertexBoneIds.x);
    int b1 = int(vertexBoneIds.y);
    int b2 = int(vertexBoneIds.z);
    int b3 = int(vertexBoneIds.w);

    mat4 skin =
        boneMatrices[b0] * vertexBoneWeights.x +
        boneMatrices[b1] * vertexBoneWeights.y +
        boneMatrices[b2] * vertexBoneWeights.z +
        boneMatrices[b3] * vertexBoneWeights.w;

    vec4 skinned = skin * vec4(vertexPosition, 1.0);

    fragTexCoord = vertexTexCoord;
    fragColor = vertexColor;
    fragNormal = normalize(vec3(matNormal * (skin * vec4(vertexNormal, 0.0))));
    gl_Position = mvp * skinned;
}
"""

# Fixed soft top-down light baked into the shader. The previous version drove the
# key/fill/colour from time-of-day *uniforms*, but on this raylib 5.5 / Apple-GL
# build set_shader_value never reached the program, so every uniform read back as
# zero and the whole character rendered solid black. Day/night now arrives as a
# per-draw tint folded into colDiffuse (see ModelRegistry.set_daylight), and this
# fixed term only adds gentle form shading — it can never go fully black.
_FS = """#version 330
in vec2 fragTexCoord;
in vec4 fragColor;
in vec3 fragNormal;

uniform sampler2D texture0;
uniform vec4 colDiffuse;

out vec4 finalColor;

void main()
{
    vec4 texel = texture(texture0, fragTexCoord);
    vec3 n = normalize(fragNormal);
    float light = 0.6 + 0.4 * clamp(n.y * 0.5 + 0.5, 0.0, 1.0);  // soft sky/ground fill
    vec3 rgb = texel.rgb * colDiffuse.rgb * fragColor.rgb * light;
    finalColor = vec4(rgb, texel.a * colDiffuse.a * fragColor.a);
}
"""


def load_skinning_shader():
    """Build the skinning shader and wire the attribute/uniform locations raylib
    needs so `update_model_animation_bones` + DrawModel feed it correctly."""
    sh = pr.load_shader_from_memory(_VS, _FS)
    sh.locs[pr.SHADER_LOC_BONE_MATRICES] = pr.get_shader_location(sh, "boneMatrices")
    sh.locs[pr.SHADER_LOC_VERTEX_POSITION] = pr.get_shader_location_attrib(sh, "vertexPosition")
    sh.locs[pr.SHADER_LOC_VERTEX_TEXCOORD01] = pr.get_shader_location_attrib(sh, "vertexTexCoord")
    sh.locs[pr.SHADER_LOC_VERTEX_NORMAL] = pr.get_shader_location_attrib(sh, "vertexNormal")
    sh.locs[pr.SHADER_LOC_MATRIX_MODEL] = pr.get_shader_location(sh, "matModel")
    sh.locs[pr.SHADER_LOC_MATRIX_NORMAL] = pr.get_shader_location(sh, "matNormal")
    sh.locs[pr.SHADER_LOC_COLOR_DIFFUSE] = pr.get_shader_location(sh, "colDiffuse")
    return sh


def apply_to_model(model, shader) -> None:
    """Assign the skinning shader to every material on a model."""
    for i in range(model.materialCount):
        model.materials[i].shader = shader
