from sklearn.ensemble import RandomForestClassifier
from sklearn.model_selection import StratifiedKFold, RepeatedStratifiedKFold, GridSearchCV
from sklearn.pipeline import Pipeline
from sklearn.metrics import confusion_matrix, roc_auc_score, mean_absolute_error, mean_squared_error
from sklearn.preprocessing import label_binarize
import matplotlib.pyplot as plt
import numpy as np
import seaborn as sns
import xgboost as xgb
import pandas as pd
import scipy
import scipy.stats
from settings import *

import warnings
warnings.filterwarnings("ignore", category=DeprecationWarning)


def print_debug(text):
    if DEBUG_USERS:
        print(text)


def compute_mean_ci(x):
    mean_x = np.mean(x)
    stderr_x = scipy.stats.sem(x)
    # ci = (mean_x-1.98*stderr_x, mean_x+1.98*stderr_x)
    return mean_x, stderr_x


def plot_confusion(gts, preds, title):
    lab = sorted(np.unique(gts))
    cmat = confusion_matrix(gts, preds, labels=lab)
    norm_cmat = cmat/np.sum(cmat, axis=1).reshape([-1, 1])
    # norm_cmat = cmat / np.sum(cmat, axis=0).reshape([1, -1])
    plt.figure()
    plt.title(title)
    sns.heatmap(cmat, annot=True, fmt='d')
    plt.xlabel('Ground Truth'), plt.ylabel('Prediction')
    plt.show()


def train_user_model(Data, label_name, model_type):
    ground_truths, preds = np.array([]), np.array([])
    scores = pd.DataFrame(columns=['subject_id', 'binned', 'AUC', 'MSE', 'MAE'])
    sorted_users = sorted(Data.subject_id.unique())
    for user in sorted_users:
        print_debug('--------------')
        print('User: %s' % user)

        # Get data belonging to a specific user
        subj_data = Data[Data.subject_id == user].copy()
        subj_data.sort_values(by='timestamp', inplace=True)

        # Remove cases where on_off is not labeled
        subj_data = subj_data[subj_data.on_off > -1]

        # Make sure there's enough data for analysis
        if len(subj_data) <= MIN_POINTS_PER_SUBJECT:
            print_debug('Not enough data points for that user')
            continue
        if min(subj_data[label_name].value_counts()) <= MIN_POINTS_PER_CLASS:
            print_debug('Not enough data points for a class with that user')
            continue

        # Separate into features and labels
        x = subj_data.iloc[:, :-7].values
        y = subj_data[label_name].values

        rskf = RepeatedStratifiedKFold(n_splits=NUM_STRATIFIED_FOLDS, n_repeats=NUM_STRATIFIED_ROUNDS, random_state=RANDOM_SEED)
        for fold_idx, (train_idxs, test_idxs) in enumerate(list(rskf.split(subj_data.ID, subj_data[label_name]))):
            print_debug('Round: %d, Fold %d' % (int(fold_idx/NUM_STRATIFIED_FOLDS)+1,
                                                (fold_idx % NUM_STRATIFIED_FOLDS)+1))
            x_train, x_test = x[train_idxs, :], x[test_idxs, :]
            y_train, y_test = y[train_idxs], y[test_idxs]

            train_classes, test_classes = np.unique(y_train), np.unique(y_test)
            id_test = subj_data.ID.values[test_idxs]

            # Check that classes align properly
            # TODO: maybe these checks can be moved before loop?
            if len(train_classes) <= 1:
                print_debug('Not enough classes in train')
                continue
            if len(test_classes) <= 1:
                print_debug('Not enough classes in test')
                continue
            if any([c not in train_classes for c in test_classes]):
                print_debug('There is a test class that is not in train')
                continue

            # Pick the correct model model
            if model_type == RANDOM_FOREST:
                model = RandomForestClassifier(random_state=RANDOM_SEED)
                param_grid = dict(regression__n_estimators=np.arange(10, 51, 10))
            elif model_type == XGBOOST:
                model = xgb.XGBClassifier(objective="multi:softprob", random_state=RANDOM_SEED)
                model.set_params(**{'num_class': len(train_classes)})
                param_grid = dict(regression__n_estimators=np.arange(80, 121, 20))
            else:
                raise Exception('Not a valid model type')

            # Identify ideal parameters using stratified k-fold cross-validation
            cross_validator = StratifiedKFold(n_splits=PARAM_SEARCH_FOLDS, random_state=RANDOM_SEED)
            pipeline = Pipeline([
                ('regression', model)
            ])
            grid_search = GridSearchCV(pipeline, param_grid=param_grid, cv=cross_validator)
            grid_search.fit(x_train, y_train)
            model = pipeline.set_params(**grid_search.best_params_)
            print_debug('Done cross-validating')

            # Fit the model and predict classes
            model.fit(x_train, y_train)
            pred = model.predict(x_test)
            probs = model.predict_proba(x_test)
            lab = model.classes_

            # Concatenate results
            ground_truths = np.concatenate([ground_truths, y_test])
            preds = np.concatenate([preds, pred])

            # Show user confusion matrix
            # if DEBUG_USERS:
            #     plot_confusion(y_test, pred, 'Model: %s, Label: %s\nSubject: %s'
            #                    % (model_type, label_name, user))

            # Bin probabilities over each diary entry
            # TODO: what does this do?
            probs_bin = []
            y_test_bin = []
            pred_bin = []
            for ID in np.unique(id_test):
                probs_bin.append(np.mean(probs[id_test == ID, :], axis=0).reshape([1, -1]))
                y_test_bin.append(np.mean(y_test[id_test == ID]))
                pred_bin.append(np.mean(pred[id_test == ID]))
            probs_bin = np.vstack(probs_bin)
            y_test_bin = np.vstack(y_test_bin)
            pred_bin = np.vstack(pred_bin)

            # Binarize the results
            y_test_binary = label_binarize(y_test, lab)
            y_test_bin_binary = label_binarize(y_test_bin, lab)

            # Drop probabilities for classes not found in test data
            for i in list(range(np.shape(y_test_binary)[1]))[::-1]:
                if not any(y_test_bin_binary[:, i]):
                    probs = np.delete(probs, i, axis=1)
                    probs_bin = np.delete(probs_bin, i, axis=1)
                    y_test_binary = np.delete(y_test_binary, i, axis=1)
                    y_test_bin_binary = np.delete(y_test_bin_binary, i, axis=1)

            # Calculate MSE and MAE
            mse = mean_squared_error(y_test, pred)
            binned_mse = mean_squared_error(y_test_bin, pred_bin)
            mae = mean_absolute_error(y_test, pred)
            binned_mae = mean_absolute_error(y_test_bin, pred_bin)

            # Calculate regular and binned AUCs
            if len(lab) > 2:
                auc = roc_auc_score(y_test_binary, probs, average='weighted')
                binned_auc = roc_auc_score(y_test_bin_binary, probs_bin, average='weighted')
            else:
                auc = roc_auc_score(y_test_binary, probs[:, 0], average='weighted')
                binned_auc = roc_auc_score(y_test_bin_binary, probs_bin[:, 0], average='weighted')
            print_debug('Regular AUC: %0.2f' % auc)
            print_debug('Binned AUC: %0.2f' % binned_auc)

            # Add scores
            scores = scores.append({'subject_id': user, 'binned': 'raw',
                                    'AUC': auc, 'MSE': mse, 'MAE': mae},
                                   ignore_index=True)
            scores = scores.append({'subject_id': user, 'binned': 'binned',
                                    'AUC': binned_auc, 'MSE': binned_mse, 'MAE': binned_mae},
                                   ignore_index=True)

    # Grab stats
    raw_aucs = scores[scores.binned == 'raw'].AUC
    raw_mses = scores[scores.binned == 'raw'].MSE
    raw_maes = scores[scores.binned == 'raw'].MAE
    binned_aucs = scores[scores.binned == 'binned'].AUC
    binned_mses = scores[scores.binned == 'binned'].MSE
    binned_maes = scores[scores.binned == 'binned'].MAE

    # Compute means and CIs
    raw_auc_mean, raw_auc_stderr = compute_mean_ci(raw_aucs)
    raw_mse_mean, raw_mse_stderr = compute_mean_ci(raw_mses)
    raw_mae_mean, raw_mae_stderr = compute_mean_ci(raw_maes)
    binned_auc_mean, binned_auc_stderr = compute_mean_ci(binned_aucs)
    binned_mse_mean, binned_mse_stderr = compute_mean_ci(binned_mses)
    binned_mae_mean, binned_mae_stderr = compute_mean_ci(binned_maes)

    # Create title
    title = 'Model: %s, Label: %s\n' % (model_type, label_name)
    title += 'Raw: AUC = %0.2f±%0.2f, MSE = %0.2f±%0.2f, MAE = %0.2f±%0.2f\n' % \
             (raw_auc_mean, raw_auc_stderr, raw_mse_mean, raw_mse_stderr, raw_mae_mean, raw_mae_stderr)
    title += 'Binned: AUC = %0.2f±%0.2f, MSE = %0.2f±%0.2f, MAE = %0.2f±%0.2f' % \
             (binned_auc_mean, binned_auc_stderr, binned_mse_mean, binned_mse_stderr, binned_mae_mean, binned_mae_stderr)

    # Plot boxplot of AUCs
    num_users = len(sorted_users)
    fig = plt.figure()
    ax = fig.add_subplot(111)
    sns.boxplot(x='subject_id', y='AUC', data=scores, hue='binned')
    plt.title(title)
    plt.axhline(0.5, 0, num_users, color='k', linestyle='--')
    ax.set_xticklabels(sorted_users), plt.setp(ax.xaxis.get_majorticklabels(), rotation=45)
    plt.xlabel('Subject ID')
    plt.ylabel('AUC'), plt.ylim(0, 1)
    plt.show()

    ttest = scipy.stats.ttest_rel(raw_aucs, binned_aucs)
    print('T-test pred-value:', ttest[1])