/*
 * Copyright (C) 2021 Kovid Goyal <kovid at kovidgoyal.net>
 *
 * Distributed under terms of the GPL3 license.
 */

#pragma once

#include "data-types.h"
#include <hb-ft.h>

bool render_single_line(const char *text, unsigned sz_px, uint32_t fg, uint32_t bg, uint8_t *output_buf, size_t width, size_t height, float x_offset, float y_offset);

typedef struct FontConfigFace {
    char *path;
    int index;
    int hinting;
    int hintstyle;
} FontConfigFace;

bool information_for_font_family(const char *family, bool bold, bool italic, FontConfigFace *ans);
FT_Face native_face_from_path(const char *path, int index);
bool fallback_font(char_type ch, const char *family, bool bold, bool italic, bool prefer_color, FontConfigFace *ans);
bool freetype_convert_mono_bitmap(FT_Bitmap *src, FT_Bitmap *dest);
FT_Library freetype_library(void);
void set_freetype_error(const char* prefix, int err_code);

void set_main_face_family(const char *family, bool bold, bool italic);
