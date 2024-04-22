# ViT-Shapley

This is the code repository for the article "Shapley-based Saliency Maps Improve Interpretability of Vertebral Compression Fractures Classification: Multicenter Study."

Here, we express our gratitude to Ian Covert, Chanwoo Kim, and Su-In Lee for their work on computing vit-shapley (https://github.com/suinleelab/vit-shapley). We made minor modifications to the code to adapt it for input from lumbar spine X-ray images.

The calculations of model accuracy, sensitivity, and specificity within the article utilized our previously developed platform, PixelMedAI(https://github.com/410312774/PixelMedAI).
## Installation

```bash
git clone https://github.com/chanwkimlab/vit-shapley.git
cd vit-shapley
pip install -r requirements.txt
```

## Training

Commands for training and testing the models are available in the files under `scripts` directory.

* scripts/training_classifier.md
* scripts/training_surrogate.md
* scripts/training_explainer.md
* scripts/training_classifier_masked.md
