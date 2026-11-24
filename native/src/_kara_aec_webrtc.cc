#include "_kara_aec_webrtc.h"

#include <array>
#include <memory>

#include <api/scoped_refptr.h>
#include <modules/audio_processing/include/audio_processing.h>

struct KaraAecWebrtc {
    explicit KaraAecWebrtc(int render_channels)
        : apm(webrtc::AudioProcessingBuilder().Create()),
          render(KARA_AEC_RATE, render_channels), capture(KARA_AEC_RATE, 1) {}

    rtc::scoped_refptr<webrtc::AudioProcessing> apm;
    webrtc::StreamConfig render;
    webrtc::StreamConfig capture;
    std::array<int16_t, KARA_AEC_FRAMES * 2> reverse_output;
};

KaraAecWebrtc *kara_aec_create(int render_channels)
{
    if (render_channels != 1 && render_channels != 2)
        return nullptr;

    try {
        auto state = std::make_unique<KaraAecWebrtc>(render_channels);
        if (!state->apm)
            return nullptr;

        // AEC3 only. Keep AGC, noise gating and the standalone high-pass
        // disabled so the prototype measures the cost of cancelling echo.
        webrtc::AudioProcessing::Config config;
        config.pipeline.maximum_internal_processing_rate = KARA_AEC_RATE;
        config.pipeline.multi_channel_render = render_channels == 2;
        config.echo_canceller.enabled = true;
        config.echo_canceller.enforce_high_pass_filtering = false;
        state->apm->ApplyConfig(config);
        return state.release();
    } catch (...) {
        return nullptr;
    }
}

void kara_aec_destroy(KaraAecWebrtc *state)
{
    delete state;
}

int kara_aec_step(KaraAecWebrtc *state,
                  const int16_t *render, size_t render_samples,
                  const int16_t *capture, size_t capture_samples,
                  int16_t *output, size_t output_samples, int delay_ms)
{
    if (!state || !render || !capture || !output ||
        render_samples != state->render.num_samples() ||
        capture_samples != KARA_AEC_FRAMES || output_samples != KARA_AEC_FRAMES ||
        delay_ms < 0 || delay_ms > 500)
        return -1;

    try {
        // Feed the actual render reference before processing its paired
        // capture. The reverse output is scratch and is never played.
        auto &apm = *state->apm;
        int error = apm.ProcessReverseStream(render, state->render, state->render,
                                             state->reverse_output.data());
        if (error != 0)
            return error;
        error = apm.set_stream_delay_ms(delay_ms);
        if (error != 0)
            return error;
        return apm.ProcessStream(capture, state->capture, state->capture, output);
    } catch (...) {
        return -1;
    }
}
