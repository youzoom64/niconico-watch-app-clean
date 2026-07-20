class NicoArchivePlayer {
  constructor(config) {
    this.config = config;
    this.audioPlayer = null;
    this.seekbar = null;
    this.autoJumpToggle = null;
    this.lastFlashedBlock = null;
    this.sentimentChart = null;

    this.init();
  }

  init() {
    this.setupElements();
    this.setupAudioPlayer();
    this.createPlayButtons();
    this.setupHeightControl();
    this.loadScreenshots();
    this.setupNicoJump();
    this.setupCommentToggles();
    this.createEmotionChart();
    this.equalizeHeights();
    this.setupImageErrorHandling();
  }

  setupElements() {
    this.audioPlayer = document.getElementById("audioPlayer");
    this.seekbar = document.getElementById("seekbar");
    this.autoJumpToggle = document.getElementById("autoJumpToggle");
  }

  setupAudioPlayer() {
    if (!this.audioPlayer) return;

    this.audioPlayer.onloadedmetadata = () => {
      this.seekbar.max = this.audioPlayer.duration;
    };

    this.seekbar.addEventListener("input", () => {
      this.audioPlayer.currentTime = this.seekbar.value;
      if (this.autoJumpToggle.checked) {
        this.scrollToCurrentTimeBlock();
      }
    });

    this.audioPlayer.addEventListener("timeupdate", () => {
      this.seekbar.value = this.audioPlayer.currentTime;
      if (this.autoJumpToggle.checked) {
        this.scrollToCurrentTimeBlock();
      }
    });
  }

  createPlayButtons() {
    const timeBlocks = document.querySelectorAll("#timeline1 .time-block");

    timeBlocks.forEach((block) => {
      const timeIndex = block.id.split("_")[2];
      const playButton = document.createElement("div");
      playButton.className = "play-button";
      playButton.innerHTML = "PLAY▶";
      playButton.style.cssText =
        "position: absolute; top: 0; right: 0; cursor: pointer;";

      playButton.addEventListener("click", () => {
        const seekTime = parseInt(timeIndex, 10);
        this.audioPlayer.currentTime = seekTime;
        this.audioPlayer.play();

        if (this.autoJumpToggle.checked) {
          this.scrollToCurrentTimeBlock();
        }
      });

      block.appendChild(playButton);
    });
  }

  scrollToCurrentTimeBlock() {
    const currentBlock = Math.floor(this.audioPlayer.currentTime / 10) * 10;
    const timeBlockId = `time_block_${currentBlock}`;
    const timeBlock1 = document.getElementById(timeBlockId);
    const timeBlock2 = document.querySelector(
      `#timeline2 .time-block[id="${timeBlockId}"]`
    );

    if (timeBlock1 && this.lastFlashedBlock !== currentBlock) {
      timeBlock1.scrollIntoView({
        behavior: "smooth",
        block: "center",
      });

      timeBlock1.classList.add("flash-fade-out");
      if (timeBlock2) {
        timeBlock2.classList.add("flash-fade-out");
      }

      setTimeout(() => {
        timeBlock1.classList.remove("flash-fade-out");
        if (timeBlock2) {
          timeBlock2.classList.remove("flash-fade-out");
        }
      }, 1000);

      this.lastFlashedBlock = currentBlock;
    }
  }

  setupHeightControl() {
    const gaugeBar = document.getElementById("gaugeBar");
    if (!gaugeBar) return;

    gaugeBar.addEventListener("input", () => {
      const gaugeValue = gaugeBar.value;
      const nearestBlockId = this.getNearestTimeBlockId();
      const nearestBlock = nearestBlockId
        ? document.getElementById(nearestBlockId)
        : null;
      const offsetTop = nearestBlock
        ? nearestBlock.getBoundingClientRect().top
        : 0;

      document.querySelectorAll(".time-block").forEach((block) => {
        block.style.height = `${gaugeValue}px`;
      });

      if (nearestBlock) {
        window.scrollBy(
          0,
          nearestBlock.getBoundingClientRect().top - offsetTop
        );
      }
    });
  }

  getNearestTimeBlockId() {
    const timeBlocks = document.querySelectorAll(".time-block");
    let nearestBlockId = null;
    let nearestDistance = Infinity;

    timeBlocks.forEach((block) => {
      const rect = block.getBoundingClientRect();
      const distance = Math.abs(rect.top);

      if (distance < nearestDistance) {
        nearestDistance = distance;
        nearestBlockId = block.id;
      }
    });

    return nearestBlockId;
  }

  loadScreenshots() {
    const { duration, screenshotPath } = this.config;

    for (let seconds = 0; seconds <= duration; seconds += 10) {
      const timeBlockId = `time_block_${seconds}`;
      const timeBlock = document.getElementById(timeBlockId);

      if (timeBlock) {
        const imgContainer = document.createElement("div");
        imgContainer.className = "img_container";

        const img = document.createElement("img");
        img.src = `${screenshotPath}/${seconds}.png`;
        img.alt = `動画のスクリーンショット ${seconds}秒`;

        imgContainer.appendChild(img);
        timeBlock.appendChild(imgContainer);
      }
    }
  }

  setupNicoJump() {
    const { lvValue } = this.config;
    const timeBlocks = document.querySelectorAll("#timeline1 .time-block");

    timeBlocks.forEach((block) => {
      const videoSecond = block.id.replace("time_block_", "");

      const nicoJumpDiv = document.createElement("div");
      nicoJumpDiv.className = "nico-jump";
      nicoJumpDiv.style.cssText =
        "position: absolute; left: 5px; bottom: 10px;";

      const jumpButton = document.createElement("button");
      jumpButton.textContent = "タイムシフトにジャンプ";
      jumpButton.onclick = () => {
        const jumpUrl = `https://live.nicovideo.jp/watch/lv${lvValue}#${videoSecond}`;
        window.open(jumpUrl, "_blank");
      };

      nicoJumpDiv.appendChild(jumpButton);
      block.style.position = "relative";
      block.appendChild(nicoJumpDiv);
    });
  }

  setupCommentToggles() {
    document.querySelectorAll(".comment-toggle").forEach((button) => {
      button.addEventListener("click", function () {
        const userId = this.getAttribute("data-user-id");
        const commentDiv = document.getElementById(userId + "-comments");
        const buttons = document.querySelectorAll(
          `button[data-user-id="${userId}"]`
        );

        if (commentDiv.style.display === "none") {
          commentDiv.style.display = "block";
          buttons[0].style.display = "none";
          buttons[1].style.display = "block";
        } else {
          commentDiv.style.display = "none";
          buttons[0].style.display = "block";
          buttons[1].style.display = "none";
        }
      });
    });
  }

  createEmotionChart() {
    const { segments, emotionData } = this.config;
    const graphContainer = document.querySelector(".graph-container");

    if (!graphContainer || !segments || !emotionData) return;

    const ctx = document.createElement("canvas");
    ctx.width = 400;
    ctx.height = 100;
    graphContainer.appendChild(ctx);

    this.sentimentChart = new Chart(ctx.getContext("2d"), {
      type: "line",
      data: {
        labels: segments,
        datasets: [
          {
            label: "Positive",
            data: emotionData.positive,
            borderColor: "green",
            borderWidth: 1,
          },
          {
            label: "Center",
            data: emotionData.center,
            borderColor: "blue",
            borderWidth: 1,
          },
          {
            label: "Negative",
            data: emotionData.negative,
            borderColor: "red",
            borderWidth: 1,
          },
        ],
      },
      options: {
        tooltips: {
          enabled: true,
          mode: "index",
          intersect: false,
          callbacks: {
            beforeBody: (tooltipItems) => {
              const segmentIndex = tooltipItems[0].index;
              return this.createTooltipText(segmentIndex);
            },
            label: (tooltipItem, data) => {
              const label = data.datasets[tooltipItem.datasetIndex].label;
              const value = tooltipItem.yLabel.toFixed(2);
              return `${label}: ${value}`;
            },
            title: () => "",
          },
        },
        onClick: (evt) => {
          const activePoints = this.sentimentChart.getElementsAtEvent(evt);
          if (activePoints.length > 0) {
            const dataIndex = activePoints[0]._index;
            this.jumpToTimeBlock(dataIndex);
          }
        },
      },
    });
  }

  createTooltipText(dataIndex) {
    const timeBlockID = this.config.segments[dataIndex];
    const commentElement = document.getElementById(`time_block_${timeBlockID}`);

    if (commentElement && commentElement.querySelector(".comment")) {
      const htmlContent = commentElement.querySelector(".comment").innerHTML;
      return htmlContent
        .replace(/<br>/g, " ")
        .replace(/<br\/>/g, " ")
        .replace(/<p>|<\/p>|<div>|<\/div>/g, "")
        .replace(/\n/g, " ")
        .trim();
    }
    return "";
  }

  jumpToTimeBlock(dataIndex) {
    const timeBlockID = this.config.segments[dataIndex];
    const timeBlockElement = document.getElementById(
      `time_block_${timeBlockID}`
    );

    if (timeBlockElement) {
      timeBlockElement.scrollIntoView({
        behavior: "smooth",
        block: "start",
      });
    }
  }

  equalizeHeights() {
    window.addEventListener("load", this.doEqualizeHeights);
    window.addEventListener("resize", this.doEqualizeHeights);
  }

  doEqualizeHeights() {
    const timeline1Blocks = document.querySelectorAll("#timeline1 .time-block");
    const timeline2Blocks = document.querySelectorAll("#timeline2 .time-block");

    for (
      let i = 0;
      i < Math.min(timeline1Blocks.length, timeline2Blocks.length);
      i++
    ) {
      const block1 = timeline1Blocks[i];
      const block2 = timeline2Blocks[i];
      const maxHeight = Math.max(block1.clientHeight, block2.clientHeight);

      block1.style.height = `${maxHeight}px`;
      block2.style.height = `${maxHeight}px`;
    }
  }

  setupImageErrorHandling() {
    document.querySelectorAll("img").forEach((img) => {
      img.onerror = function () {
        this.src =
          "https://secure-dcdn.cdn.nimg.jp/nicoaccount/usericon/defaults/blank.jpg";
      };
    });
  }
}

// 初期化
document.addEventListener("DOMContentLoaded", function () {
  if (window.NICO_ARCHIVE_CONFIG) {
    new NicoArchivePlayer(window.NICO_ARCHIVE_CONFIG);
  } else {
    console.error("NICO_ARCHIVE_CONFIG not found");
  }
});
