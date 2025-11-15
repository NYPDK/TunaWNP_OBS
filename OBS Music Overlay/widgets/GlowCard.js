;(function () {
  function initCard() {
    const cardEl = document.querySelector('.spark-card')
    const titleEl = document.getElementById('title')
    const titleCloneEl = document.getElementById('title-clone')
    const artistEl = document.getElementById('artist')
    const artistCloneEl = document.getElementById('artist-clone')
    const coverEl = document.getElementById('cover')
    const progressEl = document.getElementById('progress')
    const rootStyle = document.documentElement.style
    const titleRow = document.querySelector('.title-row')
    const artistRow = document.querySelector('.artist-row')
    let lastCover = ''
    let pendingCoverUrl = ''
    let isCardVisible = false
    const paletteCanvas = document.createElement('canvas')
    paletteCanvas.width = paletteCanvas.height = 32
    const paletteCtx = paletteCanvas.getContext('2d')
    const PALETTE_PROXY_BASE = 'http://127.0.0.1:65432/palette?url='

    const isProxyCandidate = (value) => {
      if (!value || typeof value !== 'string') {
        return false
      }
      if (
        value.startsWith('data:') ||
        value.startsWith('blob:') ||
        value.startsWith(PALETTE_PROXY_BASE)
      ) {
        return false
      }
      // Allow any other scheme and let the proxy validate it (e.g., file://).
      return true
    }

    const buildProxySource = (value) => {
      if (!isProxyCandidate(value)) {
        return null
      }
      try {
        return `${PALETTE_PROXY_BASE}${encodeURIComponent(value)}`
      } catch (err) {
        return null
      }
    }

    const getAverageColorFromImage = (image) => {
      if (!paletteCtx) {
        return null
      }

      paletteCtx.clearRect(0, 0, paletteCanvas.width, paletteCanvas.height)
      paletteCtx.drawImage(image, 0, 0, paletteCanvas.width, paletteCanvas.height)
      const { data } = paletteCtx.getImageData(0, 0, paletteCanvas.width, paletteCanvas.height)

      let r = 0
      let g = 0
      let b = 0
      let count = 0

      for (let i = 0; i < data.length; i += 4) {
        const alpha = data[i + 3]
        if (alpha === 0) {
          continue
        }

        r += data[i]
        g += data[i + 1]
        b += data[i + 2]
        count++
      }

      if (!count) {
        return null
      }

      r = Math.round(r / count)
      g = Math.round(g / count)
      b = Math.round(b / count)

      return {
        r,
        g,
        b,
        toRgba: (alpha = 1) => `rgba(${r}, ${g}, ${b}, ${alpha})`,
      }
    }

    window.getAverageColorFromImage = getAverageColorFromImage

    if (!titleEl || !artistEl || !coverEl || !progressEl) {
      return
    }

    const createTrack = (wrapper, main, clone) => ({
      wrapper,
      inner: main?.parentElement ?? null,
      main,
      clone,
    })

    const scrollTracks = [
      createTrack(titleRow, titleEl, titleCloneEl),
      createTrack(artistRow, artistEl, artistCloneEl),
    ]

    const getBrightnessValue = ({ r = 0, g = 0, b = 0 } = {}) =>
      Math.round(0.299 * r + 0.587 * g + 0.114 * b)

    const BRIGHTNESS_THRESHOLD = 128
    const BRIGHTNESS_LIGHT_COLOR = 'rgba(255, 255, 255, 0.95)'
    const BRIGHTNESS_DARK_COLOR = 'rgba(0, 0, 0, 0.8)'

    const getBrightnessColor = (color) =>
      getBrightnessValue(color) < BRIGHTNESS_THRESHOLD
        ? BRIGHTNESS_LIGHT_COLOR
        : BRIGHTNESS_DARK_COLOR

    const applyFallbackPalette = () => {
      rootStyle.setProperty('--cover-avg-color', 'rgba(255, 255, 255, 0.35)')
      rootStyle.setProperty('--cover-avg-brightness-color', 'rgba(255, 255, 255, 0.35)')
    }

    const applyCoverPalette = (image, url, { proxyTried = false } = {}) => {
      if (!paletteCtx) {
        return
      }

      try {
        const avg = getAverageColorFromImage(image)
        if (!avg) {
          if (!proxyTried) {
            const proxyUrl = buildProxySource(url)
            if (proxyUrl) {
              samplePaletteFromSource(proxyUrl, url, true, { proxyTried: true })
              return
            }
          }
          applyFallbackPalette()
          return
        }

        const brightnessColor = getBrightnessColor(avg)
        rootStyle.setProperty('--cover-avg-brightness-color', brightnessColor)
        rootStyle.setProperty('--cover-avg-color', avg.toRgba(0.95))
      } catch (err) {
        if (!proxyTried) {
          const proxyUrl = buildProxySource(url)
          if (proxyUrl) {
            samplePaletteFromSource(proxyUrl, url, true, { proxyTried: true })
            return
          }
        }
        applyFallbackPalette()
      }
    }

    const resetCoverHandlers = () => {
      coverEl.onload = null
      coverEl.onerror = null
    }

    const setCardBackdrop = (src) => {
      if (!cardEl) {
        return
      }
      if (!src) {
        cardEl.style.removeProperty('--cover-image')
        return
      }
      const safeSrc = src.replace(/"/g, '\\"')
      cardEl.style.setProperty('--cover-image', `url("${safeSrc}")`)
    }

    const setCoverEmpty = () => {
      resetCoverHandlers()
      coverEl.removeAttribute('src')
      coverEl.classList.add('empty')
      setCardBackdrop('')
      applyFallbackPalette()
      pendingCoverUrl = ''
    }

    const samplePaletteFromSource = (src, url, useCrossOrigin, options = {}) => {
      const paletteImage = new Image()
      if (useCrossOrigin) {
        paletteImage.crossOrigin = 'Anonymous'
      }
      paletteImage.onload = () => {
        if (pendingCoverUrl && pendingCoverUrl !== url) {
          return
        }
        applyCoverPalette(paletteImage, url, options)
      }
      paletteImage.onerror = () => {
        if (!options.proxyTried) {
          const proxyUrl = buildProxySource(url)
          if (proxyUrl) {
            samplePaletteFromSource(proxyUrl, url, true, { proxyTried: true })
            return
          }
        }
        applyFallbackPalette()
      }
      paletteImage.src = src
    }

    const setCoverSource = (src, url, { canSample = false, useCrossOrigin = false } = {}) => {
      resetCoverHandlers()
      coverEl.onload = () => {
        lastCover = url
        coverEl.classList.remove('empty')
        setCardBackdrop(src || url)
        if (pendingCoverUrl === url) {
          pendingCoverUrl = ''
        }
      }
      coverEl.onerror = () => {
        setCoverEmpty()
      }
      if (canSample && src) {
        samplePaletteFromSource(src, url || src, useCrossOrigin)
      }
      coverEl.src = src
      if (canSample) {
        coverEl.classList.remove('empty')
      } else {
        coverEl.classList.add('empty')
      }
    }

    const updateCover = (url) => {
      if (!url) {
        pendingCoverUrl = ''
        setCoverEmpty()
        return
      }

      if (url === lastCover || url === pendingCoverUrl) {
        return
      }

      pendingCoverUrl = url
      loadCover(url)
    }

    const isPlayingState = (mediaInfo) => {
      const state = (mediaInfo.state || '').toLowerCase()
      return state !== 'stopped' && state !== 'paused'
    }

    const updateScrollState = () => {
      scrollTracks.forEach((track) => {
        if (!track.wrapper || !track.main || !track.inner) {
          return
        }
        const wrapperWidth = track.wrapper.clientWidth
        if (!wrapperWidth) {
          track.wrapper.classList.remove('scrolling')
          track.clone?.classList?.remove('visible')
          track.inner?.style?.removeProperty('--scroll-duration')
          track.inner?.style?.removeProperty('--scroll-distance')
          return
        }
        const contentWidth = track.main.scrollWidth
        const gap = parseFloat(getComputedStyle(track.inner).gap || '0') || 0
        const needsScroll = contentWidth > wrapperWidth
        const shouldScroll = needsScroll && isCardVisible
        track.clone?.classList?.toggle('visible', shouldScroll)
        if (shouldScroll) {
          const travel = contentWidth + gap
          const ratio = travel / wrapperWidth
          const duration = Math.max(8, ratio * 4 + 6)
          track.inner.style.setProperty('--scroll-duration', `${duration}s`)
          track.inner.style.setProperty('--scroll-distance', `${travel}px`)
          track.wrapper.classList.add('scrolling')
        } else {
          track.inner.style.removeProperty('--scroll-duration')
          track.inner.style.removeProperty('--scroll-distance')
          track.wrapper.classList.remove('scrolling')
        }
      })
    }

    const setVisibility = (show) => {
      if (!cardEl) {
        return
      }
      isCardVisible = Boolean(show)
      cardEl.classList.toggle('is-visible', isCardVisible)
      if (!isCardVisible) {
        scrollTracks.forEach((track) => {
          track.clone?.classList?.remove('visible')
          track.wrapper?.classList?.remove('scrolling')
          track.inner?.style?.removeProperty('--scroll-duration')
          track.inner?.style?.removeProperty('--scroll-distance')
        })
      }
    }

    const scheduleScrollUpdate = () => {
      requestAnimationFrame(() => {
        requestAnimationFrame(updateScrollState)
      })
    }

    const loadCover = (url) => {
      const useCrossOrigin = url.startsWith('http')
      setCoverSource(url, url, { canSample: true, useCrossOrigin })
    }

    registerSocket((mediaInfo) => {
      const title = (mediaInfo.title || '').trim()
      const artists = Array.isArray(mediaInfo.artists)
        ? mediaInfo.artists.filter(Boolean)
        : []
      const artistValue = artists.length
        ? artists.join(', ')
        : mediaInfo.artist || 'N/A'
      const displayTitle = title || 'N/A'
      titleEl.innerText = displayTitle
      if (titleCloneEl) {
        titleCloneEl.innerText = displayTitle
      }
      artistEl.innerText = artistValue
      if (artistCloneEl) {
        artistCloneEl.innerText = artistValue
      }
      const rawPercent =
        mediaInfo.position_percent ??
        mediaInfo.positionPercent ??
        0
      const percent = Math.max(0, Math.min(100, Number(rawPercent) || 0))
      progressEl.style.width = `${percent}%`
      updateCover(mediaInfo.cover_url || mediaInfo.coverUrl || '')
      const playing = !!title && isPlayingState(mediaInfo)
      setVisibility(playing)
      scheduleScrollUpdate()
    })
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', initCard)
  } else {
    initCard()
  }
})()
