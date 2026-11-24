#ifndef KARA_AEC_WEBRTC_H
#define KARA_AEC_WEBRTC_H

#include <stddef.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

enum { KARA_AEC_RATE = 48000, KARA_AEC_FRAMES = 480 };
typedef struct KaraAecWebrtc KaraAecWebrtc;

/* Private prototype ABI. Create returns an owned handle or NULL. The caller
 * serializes calls and destroys the handle at a timeline discontinuity.
 * Render may be mono or stereo, interleaved. Capture remains mono. */
KaraAecWebrtc *kara_aec_create(int render_channels);
void kara_aec_destroy(KaraAecWebrtc *state);

/* All counts are int16 samples. Each input and output spans precisely 10 ms.
 * The delay describes the supplied reference timeline after coarse alignment.
 * Arrays are borrowed during the call and must not overlap. Zero is success.
 * On error the output is invalid. Discard the handle before starting again. */
int kara_aec_step(KaraAecWebrtc *state,
                  const int16_t *render, size_t render_samples,
                  const int16_t *capture, size_t capture_samples,
                  int16_t *output, size_t output_samples, int delay_ms);

#ifdef __cplusplus
}
#endif

#endif
