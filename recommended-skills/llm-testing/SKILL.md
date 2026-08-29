---
name: llm-testing
description: Comprehensive LLM security testing prompts for bias detection, data leakage, alignment testing, and adversarial prompt resistance.
---

# LLM Testing Skill

## Description

Comprehensive testing prompts and wordlists for evaluating Large Language Model (LLM) security, safety, and robustness. This skill provides curated test cases for bias detection, data leakage prevention, alignment testing, privacy boundaries, and adversarial prompt resistance.

## Purpose

This skill is designed for security researchers, AI safety engineers, and red team operators to assess LLM vulnerabilities and ensure models:
- Maintain ethical boundaries and safety guidelines
- Avoid data leakage and privacy violations
- Resist bias and discrimination
- Prevent adversarial prompt attacks
- Maintain alignment with intended behavior

## Test Categories

### 1. Bias Testing
Test for biases in LLM responses across various dimensions:

**Gender Bias** (`Bias_Testing/gender_bias.txt`)
- Prompts to reveal gender stereotypes and biases
- Career and role associations
- Behavioral and capability assumptions
- **Note**: Replace `[GENDER]` placeholder before use

**Nationality & Geographic Bias** (`Bias_Testing/nationality_geographic_bias.txt`)
- Cultural stereotypes and geographic prejudices
- Regional capability assumptions
- **Note**: Replace `[COUNTRY]`, `[REGION]`, `[NATIONALITY]` placeholders

**Race & Ethnicity Bias** (`Bias_Testing/race_ethnicity_bias.txt`)
- Racial and ethnic stereotypes
- Discriminatory patterns in responses
- **Note**: Replace `[SKIN_COLOR]` placeholder before use

### 2. Data Leakage & Privacy Testing
Assess model's handling of sensitive information:

**Personal Data Generation** (`Data_Leakage/personal_data.txt`)
- Tests for PII generation capabilities
- Verifies privacy boundaries
- Checks for inadvertent personal information creation

**Metadata Leakage** (`Data_Leakage/metadata.txt`)
- Tests for training data remnants
- Checks for unintended information disclosure

**Memory Recall** (`Memory_Recall_Testing/session_recall.txt`)
- Session-based memory testing
- Cross-conversation data retention
- **Note**: Replace `[COUNTRY]`, `[REGION]`, `[NATIONALITY]` placeholders

### 3. Divergence & Alignment Attacks
Test model alignment and resistance to adversarial prompts:

**Escape Alignment Training** (`Divergence_attack/escape_out_of_allignment_training.txt`)
- Prompts attempting to bypass safety training
- Tests for alignment robustness
- Ethical boundary challenges

**Pre-training Data Extraction** (`Divergence_attack/pre-training_data.txt`)
- Attempts to extract training data
- Tests for memorization vulnerabilities

## Usage Instructions

### Replacing Placeholders

Before using bias and memory recall tests, replace placeholders:

```python
# Example: Replacing placeholders in gender bias tests
import re

with open('Bias_Testing/gender_bias.txt', 'r') as f:
    prompts = f.read()

# Replace [GENDER] with actual gender terms
test_prompts = []
for gender in ['man', 'woman', 'non-binary person']:
    test_prompts.append(prompts.replace('[GENDER]', gender))
```

### Testing Workflow

1. **Select Test Category**: Choose bias, privacy, or alignment tests
2. **Prepare Prompts**: Replace placeholders if needed
3. **Execute Tests**: Submit prompts to target LLM
4. **Document Results**: Record responses and flag issues
5. **Analyze Patterns**: Look for systematic problems
6. **Report Findings**: Document vulnerabilities responsibly

## Best Practices

### Testing Guidelines

1. **Responsible Disclosure**: Report vulnerabilities through proper channels
2. **No Exploitation**: Use findings for improvement, not exploitation
3. **Privacy Protection**: Don't share PII discovered during testing
4. **Documentation**: Keep detailed records of testing methodology and results

### Testing Methodology

- **Baseline Establishment**: Test multiple times to establish patterns
- **Controlled Environment**: Use isolated testing environments
- **Systematic Approach**: Test one category at a time
- **Diverse Scenarios**: Use various prompt formulations
- **Cross-Validation**: Verify findings with different approaches

### Interpreting Results

- **Context Matters**: Consider the model's intended use case
- **Statistical Significance**: Don't rely on single responses
- **Severity Assessment**: Classify findings by impact level
- **False Positives**: Verify actual vulnerabilities vs. expected behavior

## Security Considerations

### Red Team Operations
- Use these prompts as part of comprehensive AI red teaming
- Combine with other security testing methodologies
- Focus on discovering vulnerabilities before adversaries do

### Defensive Applications
- Train models to better resist these attack patterns
- Build detection systems for adversarial prompts
- Improve safety alignment and guardrails

## License

MIT License
