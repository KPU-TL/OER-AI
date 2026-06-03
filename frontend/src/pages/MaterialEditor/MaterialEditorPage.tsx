import { useState, useEffect, useMemo } from "react";
import { MCQEditableContainer } from "@/components/MaterialEditorPage/MCQEditableContainer";
import { EssayEditableContainer } from "@/components/MaterialEditorPage/EssayEditableContainer";
import { FlashcardEditableContainer } from "@/components/MaterialEditorPage/FlashcardEditableContainer";
import type {
  I5HPMultiChoiceQuestion,
  I5HPEssayQuestion,
  IH5PFlashcard,
  IH5PQuestion,
} from "@/types/MaterialEditor";
import {
  isMultiChoiceQuestion,
  isEssayQuestion,
  isFlashcard,
} from "@/types/MaterialEditor";
import { Card, CardDescription } from "@/components/ui/card";
import { MaterialEditorForm } from "@/components/MaterialEditorPage/MaterialEditorForm";
import { useTextbookView } from "@/providers/textbookView";
import { usePracticeMaterialStream } from "@/hooks/usePracticeMaterialStream";
import { Progress } from "@/components/ui/progress";

// Status display mapping
const STATUS_LABELS: Record<string, string> = {
  idle: "",
  initializing: "Initializing models...",
  retrieving: "Retrieving relevant content...",
  generating: "Generating practice material...",
  validating: "Validating response...",
  complete: "Complete!",
  error: "Error occurred",
};

export default function MaterialEditorPage() {
  const [mcqQuestionSets, setMcqQuestionSets] = useState<
    I5HPMultiChoiceQuestion[][]
  >([]);
  const [essayQuestionSets, setEssayQuestionSets] = useState<
    I5HPEssayQuestion[][]
  >([]);
  const [flashcardSets, setFlashcardSets] = useState<IH5PFlashcard[]>([]);
  const [errorMsg, setErrorMsg] = useState<string | null>(null);
  const { textbook } = useTextbookView();

  // WebSocket authentication token
  const [wsToken, setWsToken] = useState<string | null>(null);

  // Fetch authentication token on mount
  useEffect(() => {
    const apiEndpoint = import.meta.env.VITE_API_ENDPOINT;
    if (!apiEndpoint) return;

    let isActive = true;
    let refreshTimeoutId: number | undefined;
    const refreshDelayMs = 14 * 60 * 1000; // Refresh before 15 min expiry

    async function fetchToken() {
      if (!isActive) return;

      try {
        const response = await fetch(`${apiEndpoint}/user/publicToken`);
        if (!response.ok) throw new Error("Token request failed");

        const { token } = await response.json();
        if (!isActive) return;

        setWsToken(token);
        if (isActive) {
          refreshTimeoutId = window.setTimeout(fetchToken, refreshDelayMs);
        }
      } catch (error) {
        console.error("[MaterialEditor] Failed to fetch token:", error);
        if (!isActive) return;

        setWsToken(null);
        if (isActive) {
          refreshTimeoutId = window.setTimeout(fetchToken, 30000);
        }
      }
    }

    fetchToken();

    return () => {
      isActive = false;
      if (refreshTimeoutId) window.clearTimeout(refreshTimeoutId);
    };
  }, []);

  // Build authenticated WebSocket URL
  const baseWsUrl = import.meta.env.VITE_WEBSOCKET_URL;
  const wsUrl = useMemo(() => {
    if (!baseWsUrl || !wsToken) return null;

    try {
      const url = new URL(baseWsUrl);
      url.searchParams.set("token", wsToken);
      return url.toString();
    } catch {
      return null;
    }
  }, [baseWsUrl, wsToken]);

  // Use the streaming hook with authenticated URL
  const {
    generate,
    status,
    progress,
    result,
    error: streamError,
    isConnected,
  } = usePracticeMaterialStream(wsUrl);

  const handleQuizDelete = (index: number) => {
    const newQuestionSets = mcqQuestionSets.filter((_, i) => i !== index);
    setMcqQuestionSets(newQuestionSets);
  };

  const handleEssayDelete = (index: number) => {
    const newQuestionSets = essayQuestionSets.filter((_, i) => i !== index);
    setEssayQuestionSets(newQuestionSets);
  };

  const handleFlashcardDelete = (index: number) => {
    const newFlashcardSets = flashcardSets.filter((_, i) => i !== index);
    setFlashcardSets(newFlashcardSets);
  };

  const handleGenerate = async (formData: any) => {
    console.log("Generate form data:", formData);
    setErrorMsg(null);

    if (!textbook?.id) {
      setErrorMsg(
        "Please select a textbook before generating practice materials."
      );
      return;
    }

    // Map form data to streaming hook params
    const materialType = formData.materialType === "flashcards"
      ? "flashcard"
      : formData.materialType === "shortAnswer"
        ? "short_answer"
        : "mcq";

    generate({
      textbook_id: textbook.id,
      topic: formData.topic,
      material_type: materialType,
      difficulty: formData.difficulty,
      num_questions: formData.numQuestions,
      num_options: formData.numOptions,
      num_cards: formData.numCards,
      card_type: formData.cardType,
      force_fresh: true,
    });
  };

  // Handle successful result from WebSocket streaming
  useEffect(() => {
    if (status === "complete" && result) {
      const data = result as any;
      
      // Determine material type from result
      if (data.cards) {
        // Convert flashcards to H5P format
        const h5pFlashcard: IH5PFlashcard = {
          library: "H5P.Flashcards 1.5",
          params: {
            cards: data.cards.map((card: any) => ({
              text: card.front || card.text || "",
              answer: card.back || card.answer || "",
              tip: card.hint || card.tip || "",
            })),
            description: data.title || "Flashcard Set",
          },
        };
        setFlashcardSets((prev) => [h5pFlashcard, ...prev]);
      } else if (data.questions && data.questions[0]?.sampleAnswer) {
        // Short answer questions
        const h5pQuestions: I5HPEssayQuestion[] = data.questions.map(
          (q: any) => ({
            library: "H5P.Essay 1.5",
            params: {
              taskDescription:
                q.questionText + (q.context ? `\n\nContext: ${q.context}` : ""),
              keywords: (q.keyPoints || []).map((kp: string) => ({
                keyword: kp,
                alternatives: [],
                options: {
                  points: 1,
                  occurrences: 1,
                  caseSensitive: false,
                  forgiveMistakes: true,
                  feedbackIncluded: `Good! You mentioned: ${kp}`,
                  feedbackMissed: `Consider including: ${kp}`,
                  feedbackIncludedWord: "keyword" as const,
                  feedbackMissedWord: "keyword" as const,
                },
              })),
            },
          })
        );
        setEssayQuestionSets((prev) => [h5pQuestions, ...prev]);
      } else if (data.questions) {
        // MCQ questions
        const h5pQuestions: I5HPMultiChoiceQuestion[] = data.questions.map(
          (q: any) => ({
            library: "H5P.MultiChoice 1.17",
            params: {
              question: q.questionText,
              answers: q.options.map((opt: any) => ({
                text: opt.text,
                correct: opt.id === q.correctAnswer,
                tipsAndFeedback: {
                  tip: "",
                  chosenFeedback: opt.explanation || "",
                  notChosenFeedback: "",
                },
              })),
            },
          })
        );
        setMcqQuestionSets((prev) => [h5pQuestions, ...prev]);
      }
    }
  }, [status, result]);

  // Handle errors from streaming
  useEffect(() => {
    if (streamError) {
      setErrorMsg(streamError);
    }
  }, [streamError]);

  const handleExportToH5P = (questions: IH5PQuestion[]) => {
    // Determine question type and handle accordingly
    if (questions.length === 0) {
      console.warn("No questions to export");
      return;
    }

    const firstQuestion = questions[0];
    if (isMultiChoiceQuestion(firstQuestion)) {
      console.log("Exporting MCQ questions:", questions);
      // TODO: call api to export MCQ questions as h5p
    } else if (isEssayQuestion(firstQuestion)) {
      console.log("Exporting Essay questions:", questions);
      // TODO: call api to export Essay questions as h5p
    } else if (isFlashcard(firstQuestion)) {
      console.log("Exporting Flashcard questions:", questions);
      // TODO: call api to export Flashcard questions as h5p
    } else {
      console.error(
        "Unknown question type:",
        (firstQuestion as IH5PQuestion).library
      );
    }
  };

  // Get status label
  const statusLabel = STATUS_LABELS[status] || "";
  const isProcessing = status !== "idle" && status !== "complete" && status !== "error";

  return (
    <div className="w-full max-w-[1800px] px-4 py-4">
      <div className="min-h-screen flex flex-col md:flex-row md:items-start md:justify-center gap-6">
        <div className="w-full md:w-[30%]">
          <MaterialEditorForm onGenerate={handleGenerate} isProcessing={isProcessing} />

          {/* Connection warning */}
          {!isConnected && !isProcessing && (
            <p className="text-sm text-amber-600 mt-2">
              ⚠️ Connecting to server... Please wait.
            </p>
          )}

          {errorMsg && (
            <p className="text-sm text-destructive mt-2">{errorMsg}</p>
          )}
        </div>

        <div className="w-full md:w-[70%] space-y-6">
          <h2 className="text-2xl font-semibold">Practice Questions</h2>
          {/* Show progress bar when generating, hide empty state message */}
          {isProcessing ? (
            <Card>
              <div className="p-6 space-y-4">
                <div className="flex justify-between text-sm text-muted-foreground">
                  <span>{statusLabel}</span>
                  <span>{progress}%</span>
                </div>
                <Progress value={progress} className="h-3" />
                <p className="text-center text-sm text-muted-foreground">
                  Generating practice materials...
                </p>
              </div>
            </Card>
          ) : mcqQuestionSets.length === 0 &&
          essayQuestionSets.length === 0 &&
          flashcardSets.length === 0 ? (
            <Card>
              <CardDescription className="flex flex-col justify-center items-center p-6">
                <p className="text-center text-muted-foreground">
                  No practice materials have been generated for this session
                </p>
                <p className="text-destructive text-center mt-2">
                  Reminder: All Sessions are temporary and will not persist
                  after exiting
                </p>
              </CardDescription>
            </Card>
          ) : (
            <>
              {mcqQuestionSets.length > 0 && (
                <div className="space-y-4">
                  <h3 className="text-xl font-semibold">
                    Multiple Choice Questions
                  </h3>
                  {mcqQuestionSets.map((questions, index) => (
                    <MCQEditableContainer
                      key={`mcq-${index}`}
                      initialQuestions={questions}
                      exportToH5P={handleExportToH5P}
                      onDelete={() => {
                        handleQuizDelete(index);
                      }}
                      textbookId={textbook?.id}
                    />
                  ))}
                </div>
              )}

              {essayQuestionSets.length > 0 && (
                <div className="space-y-4">
                  <h3 className="text-xl font-semibold">Essay Questions</h3>
                  {essayQuestionSets.map((questions, index) => (
                    <EssayEditableContainer
                      key={`essay-${index}`}
                      initialQuestions={questions}
                      exportToH5P={handleExportToH5P}
                      onDelete={() => {
                        handleEssayDelete(index);
                      }}
                      textbookId={textbook?.id}
                    />
                  ))}
                </div>
              )}

              {flashcardSets.length > 0 && (
                <div className="space-y-4">
                  <h3 className="text-xl font-semibold">Flashcards</h3>
                  {flashcardSets.map((flashcard, index) => (
                    <FlashcardEditableContainer
                      key={`flashcard-${index}`}
                      initialFlashcards={flashcard}
                      exportToH5P={handleExportToH5P}
                      onDelete={() => {
                        handleFlashcardDelete(index);
                      }}
                      textbookId={textbook?.id}
                    />
                  ))}
                </div>
              )}
            </>
          )}
        </div>
      </div>
    </div>
  );
}
