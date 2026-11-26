/* Offline adapter for the SPA engine used by module-echo-cancel.
 * No server context, streams, devices or session-manager objects are opened.
 */
#include <errno.h>
#include <math.h>
#include <stdint.h>
#include <stdlib.h>
#include <pipewire/pipewire.h>
#include <spa/interfaces/audio/aec.h>
#include <spa/utils/names.h>

enum { RATE = 48000, FRAMES = 480 };

struct kara_pipewire_aec {
    struct spa_handle *handle;
    struct spa_audio_aec *aec;
    uint32_t channels;
};

void kara_pw_aec_destroy(struct kara_pipewire_aec *state)
{
    if (!state)
        return;
    if (state->handle)
        pw_unload_spa_handle(state->handle);
    pw_deinit();
    free(state);
}

int kara_pw_aec_create(uint32_t channels, struct kara_pipewire_aec **out)
{
    if (!out || (channels != 1 && channels != 2))
        return -EINVAL;
    *out = NULL;
    struct kara_pipewire_aec *state = calloc(1, sizeof(*state));
    if (!state)
        return -ENOMEM;
    pw_init(NULL, NULL);
    state->channels = channels;
    struct spa_support support[32];
    uint32_t n_support = pw_get_support(support, SPA_N_ELEMENTS(support));
    state->handle = pw_load_spa_handle("aec/libspa-aec-webrtc", SPA_NAME_AEC, NULL,
                                       n_support, support);
    void *interface = NULL;
    int status = state->handle ?
        spa_handle_get_interface(state->handle, SPA_TYPE_INTERFACE_AUDIO_AEC, &interface) : -ENOENT;
    if (status >= 0) {
        state->aec = interface;
        struct spa_audio_info_raw rec = {.format = SPA_AUDIO_FORMAT_F32P, .rate = RATE,
                                         .channels = 1, .position = {SPA_AUDIO_CHANNEL_MONO}};
        struct spa_audio_info_raw render = rec;
        render.channels = channels;
        if (channels == 2) {
            render.position[0] = SPA_AUDIO_CHANNEL_FL;
            render.position[1] = SPA_AUDIO_CHANNEL_FR;
        }
        struct spa_audio_info_raw result = rec;
        /* Compare echo cancellation alone. The graph normally also owns
         * timing and can request other processing through these options.
         */
        const struct spa_dict_item items[] = {
            SPA_DICT_ITEM_INIT("webrtc.high_pass_filter", "false"),
            SPA_DICT_ITEM_INIT("webrtc.noise_suppression", "false"),
            SPA_DICT_ITEM_INIT("webrtc.gain_control", "false"),
            SPA_DICT_ITEM_INIT("webrtc.voice_detection", "false"),
            SPA_DICT_ITEM_INIT("webrtc.transient_suppression", "false"),
        };
        const struct spa_dict config = SPA_DICT_INIT_ARRAY(items);
        status = spa_audio_aec_init2(state->aec, &config, &render, &rec, &result);
    }
    if (status < 0) {
        kara_pw_aec_destroy(state);
        return status;
    }
    *out = state;
    return 0;
}

int kara_pw_aec_step(struct kara_pipewire_aec *state, const int16_t *far, size_t far_count,
                     const int16_t *near, size_t near_count, int16_t *out, size_t out_count)
{
    if (!state || !far || !near || !out || far_count != state->channels * FRAMES ||
        near_count != FRAMES || out_count != FRAMES)
        return -EINVAL;
    float play[2][FRAMES], capture[FRAMES], result[FRAMES] = {0};
    /* All candidates receive the same s16 frames. SPA requires planar
     * float. Division by a power of two represents every s16 sample exactly.
     */
    for (uint32_t i = 0; i < FRAMES; i++) {
        capture[i] = near[i] / 32768.0f;
        for (uint32_t c = 0; c < state->channels; c++)
            play[c][i] = far[i * state->channels + c] / 32768.0f;
    }
    const float *play_ptr[] = {play[0], play[1]};
    const float *capture_ptr[] = {capture};
    float *result_ptr[] = {result};
    int status = spa_audio_aec_run(state->aec, capture_ptr, play_ptr, result_ptr, FRAMES);
    if (status < 0)
        return status;
    for (uint32_t i = 0; i < FRAMES; i++) {
        if (!isfinite(result[i]))
            return -EDOM;
        out[i] = (int16_t)lrintf(fmaxf(-32768.0f, fminf(32767.0f, result[i] * 32768.0f)));
    }
    return 0;
}
